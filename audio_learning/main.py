"""HTTP boundary for Zouma, a conversation-native learning application."""

import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .ingestion import SourceError, extract_upload, text_chunks
from .providers import CompatibleProvider, DemoProvider, LearningProvider, ProviderError, demo_exercise
from .schemas import AskQuestion, Course, CreateCourse, Session, UpdateSession
from .storage import ConflictError, NotFoundError, Store, now
from .orchestration import InvalidCommand, OrchestrationConflict, Orchestrator
from .voice import OpenAIRealtimeProvider, VoiceError, VoiceProvider


class VoiceConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sdp: str = Field(min_length=10, max_length=200000)
    connection_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=0)


DEMO_TEXT = """# 从直觉理解线性回归
线性回归用一条直线描述输入 x 和输出 y 的关系。模型写作 y 预测值等于 w 乘以 x 加上 b。w 是斜率，b 是截距。比如输入学习小时数，输出练习成绩。模型参数需要从样本中学习，而不是凭感觉指定。

# 用均方误差衡量偏差
先计算预测值与真实值的差，再平方，最后对所有样本取平均，就得到均方误差。平方使正负误差不会抵消，也会放大较大的误差。均方误差越小，表示模型在这些训练样本上的拟合误差越小。训练误差小不保证对新样本预测准确。

# 梯度下降让误差减小
梯度描述参数改变时损失的局部变化方向。梯度下降沿梯度的反方向更新参数。学习率决定每次走多远；过大可能越过低点甚至发散，过小会收敛缓慢。对固定截距 b 等于零的模型，w 的梯度是所有样本中二乘以预测误差再乘以 x 的平均值。

# 用 Python 验证一次更新
现在使用三个输入样本一、二、三，对应输出二、四、六。固定 b 等于零，从 w 等于零出发，学习率取零点一。计算一次梯度，更新 w，再比较更新前后的均方误差。一次更新后的误差应当更低，但参数还不一定正好等于二。导出的 Notebook 提供练习代码和单独的参考答案。
"""


def validate_cursor(course: Course, segment_index: int, sentence_index: int, status: str = "paused") -> None:
    if segment_index >= len(course.segments) or sentence_index >= len(course.segments[segment_index].script):
        raise HTTPException(422, "播放位置超出课程范围。")
    if status == "completed" and (
        segment_index != len(course.segments) - 1 or
        sentence_index != len(course.segments[-1].script) - 1
    ):
        raise HTTPException(422, "只有最后一段的最后一句可以标记为完成。")


def notebook_for(course: Course) -> dict:
    cells: list[dict] = []

    def markdown(text: str) -> dict:
        return {"cell_type": "markdown", "metadata": {}, "source": text}

    def code(text: str) -> dict:
        return {"cell_type": "code", "metadata": {}, "source": text, "execution_count": None, "outputs": []}

    cells.append(markdown(f"# {course.title}\n\n由走马（Zouma）导出。代码尚未执行；请在你自己的 Python 环境中阅读和运行。"))
    for index, segment in enumerate(course.segments, 1):
        cells.append(markdown(f"## {index}. {segment.title}\n\n" + "\n\n".join(segment.script) +
                              f"\n\n**原文位置：** {segment.source_locator}"))
        if segment.exercise:
            cells.append(markdown(f"### Python 练习\n\n{segment.exercise.prompt}"))
            cells.append(code(segment.exercise.starter_code))
            cells.append(markdown("### 参考答案\n\n建议先独立完成练习，再查看下一单元。"))
            cells.append(code(segment.exercise.solution_code))
    if not any(segment.exercise for segment in course.segments):
        cells.append(markdown("## 独立实践区\n\n本教材暂未生成 Python 练习。请根据课程知识设计一个可以验证的计算或模拟。"))
        cells.append(code("# 在这里编写你的 Python 实践。\n"))
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.11"},
    }, "cells": [{"id": uuid4().hex[:12], **cell} for cell in cells]}


def create_app(data_dir: str | Path | None = None, provider: LearningProvider | None = None,
               voice_provider: VoiceProvider | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if provider is None:
        if settings.provider == "demo":
            provider = DemoProvider()
        elif settings.provider == "compatible":
            provider = CompatibleProvider(settings)
        else:
            raise ValueError("AFL_PROVIDER 仅支持 demo 或 compatible。")
    store = Store(Path(data_dir) if data_dir is not None else settings.data_dir)
    orchestrator = Orchestrator(store)
    if settings.voice_provider not in {"disabled", "openai-realtime"}:
        raise ValueError("AFL_VOICE_PROVIDER 仅支持 disabled 或 openai-realtime。")
    voice = voice_provider or (OpenAIRealtimeProvider(settings) if settings.voice_provider == "openai-realtime" else None)
    application = FastAPI(
        title="走马 · Zouma",
        description="Conversation-Native Adaptive Learning System · 对话原生自适应学习系统。行在途中，思入深处。",
        version="0.2.0",
    )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    application.state.store = store
    application.state.provider = provider
    application.state.settings = settings
    application.state.orchestrator = orchestrator
    application.state.voice = voice
    web_dir = Path(__file__).parent / "web"

    @application.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # Block cross-origin browser writes to this local, single-user service.
        # No Origin is normal for CLI clients and the test client.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != expected):
                return JSONResponse(status_code=403, content={"detail": "本地服务只接受同源页面的修改请求。"})
        return await call_next(request)

    @application.exception_handler(NotFoundError)
    async def not_found(_request, _exc):
        return JSONResponse(status_code=404, content={"detail": "找不到这门课程。"})

    @application.exception_handler(ConflictError)
    async def conflict(_request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": {
            "message": "课程进度已更新，请使用最新位置重试。", "session": exc.session.model_dump(),
        }})

    @application.exception_handler(SourceError)
    async def source_error(_request, exc: SourceError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @application.exception_handler(ProviderError)
    async def provider_error(_request, exc: ProviderError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @application.exception_handler(VoiceError)
    async def voice_error(_request, exc: VoiceError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @application.exception_handler(OrchestrationConflict)
    async def learning_conflict(_request, exc: OrchestrationConflict):
        return JSONResponse(status_code=409, content={"detail": {
            "message": "学习状态已变化，请从最新检查点重新连接。", "state": exc.state,
        }})

    @application.exception_handler(InvalidCommand)
    async def invalid_command(_request, exc: InvalidCommand):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def build_course(title: str, source_name: str, chunks, selected_provider: LearningProvider) -> Course:
        try:
            segments = selected_provider.build_segments(title, chunks)
            course = Course(id=uuid4().hex, title=title, source_name=source_name, created_at=now(),
                            provider=selected_provider.name, segments=segments)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("课程生成失败，没有保存不完整的课程。请检查模型配置或缩短教材。") from exc
        return store.add_course(course)

    @application.get("/health")
    def health():
        return {"status": "ok"}

    @application.get("/api/config")
    def config():
        return {"provider": provider.name, "mode": "demo" if provider.name == "demo" else "api",
                "model": settings.model or None if provider.name != "demo" else None,
                "voice": {"provider": voice.name if voice else "disabled",
                          "configured": bool(voice and voice.configured),
                          "model": settings.realtime_model if voice else None,
                          "checkpoint_granularity": "sentence"},
                "limits": {"max_upload_bytes": settings.max_upload_bytes,
                           "max_source_chars": settings.max_source_chars,
                           "max_pdf_pages": settings.max_pdf_pages}}

    @application.get("/api/courses")
    def list_courses():
        return store.list_courses()

    @application.post("/api/courses", status_code=201, response_model=Course)
    def create_course(body: CreateCourse):
        return build_course(body.title, "粘贴文本", text_chunks(body.source_text), provider)

    @application.post("/api/courses/upload", status_code=201, response_model=Course)
    def upload_course(file: UploadFile = File(...), title: str | None = Form(None)):
        filename = Path(file.filename or "untitled.txt").name
        actual_title = (title or Path(filename).stem).strip()
        if not actual_title or len(actual_title) > 160:
            raise HTTPException(422, "课程标题需为 1–160 字。")
        data = file.file.read(settings.max_upload_bytes + 1)
        chunks = extract_upload(filename, data, settings)
        return build_course(actual_title, filename, chunks, provider)

    @application.post("/api/demo", status_code=201, response_model=Course)
    def demo():
        demo_provider = DemoProvider()
        course = Course(id=uuid4().hex, title="线性回归：从直觉到 Python", source_name="内置演示教材",
                        created_at=now(), provider="demo", segments=demo_provider.build_segments(
                            "线性回归：从直觉到 Python", text_chunks(DEMO_TEXT)))
        course.segments[-1].exercise = demo_exercise()
        return store.add_course(course)

    @application.get("/api/courses/{course_id}", response_model=Course)
    def get_course(course_id: str):
        return store.get_course(course_id)

    @application.get("/api/courses/{course_id}/session", response_model=Session)
    def get_session(course_id: str):
        return store.get_session(course_id)

    @application.get("/api/courses/{course_id}/learning")
    def get_learning(course_id: str):
        return orchestrator.get(course_id)

    @application.post("/api/courses/{course_id}/learning/commands")
    def learning_command(course_id: str, body: dict):
        return orchestrator.command(course_id, body)

    @application.get("/api/courses/{course_id}/learning/events")
    def learning_events(course_id: str):
        return orchestrator.events(course_id)

    @application.post("/api/courses/{course_id}/voice/connect")
    def connect_voice(course_id: str, body: VoiceConnect):
        current = orchestrator.get(course_id)
        if current["connection_id"] != body.connection_id or current["revision"] != body.revision:
            raise OrchestrationConflict(current)
        if voice is None or not voice.configured:
            raise HTTPException(503, "实时语音未配置。可使用文字提问和浏览器朗读。")
        if not body.sdp.startswith("v=0"):
            raise HTTPException(422, "需要有效的 WebRTC SDP offer。")
        sdp = voice.connect(body.sdp)
        # A course switch/second tab can revoke ownership during negotiation.
        latest = orchestrator.get(course_id)
        if latest["connection_id"] != body.connection_id or latest["revision"] != body.revision:
            raise OrchestrationConflict(latest)
        return Response(content=sdp, media_type="application/sdp", headers={"Cache-Control": "no-store"})

    @application.put("/api/courses/{course_id}/session", response_model=Session)
    def update_session(course_id: str, body: UpdateSession):
        course = store.get_course(course_id)
        validate_cursor(course, body.segment_index, body.sentence_index, body.status)
        return store.update_session(course_id, body.segment_index, body.sentence_index, body.status, body.revision)

    @application.post("/api/courses/{course_id}/questions", response_model=Session)
    def question(course_id: str, body: AskQuestion):
        course = store.get_course(course_id)
        validate_cursor(course, body.segment_index, body.sentence_index)
        checkpoint = store.update_session(course_id, body.segment_index, body.sentence_index,
                                          "answering", body.revision, "question_started")
        selected_provider = DemoProvider() if course.provider == "demo" else provider
        try:
            answer = selected_provider.answer(course, body.segment_index, body.sentence_index, body.question, checkpoint.qa)
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 20000:
                raise ProviderError("模型回答为空或过长，请重试。")
        except Exception as exc:
            store.fail_question(course_id, checkpoint.revision)
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("问答服务失败；提问位置已保留，请稍后重试。") from exc
        return store.finish_question(course_id, body.question, answer, body.segment_index, body.sentence_index,
                                     course.segments[body.segment_index].source_locator, checkpoint.revision)

    @application.get("/api/courses/{course_id}/notebook")
    def export_notebook(course_id: str):
        course = store.get_course(course_id)
        return Response(content=json.dumps(notebook_for(course), ensure_ascii=False, indent=2),
                        media_type="application/x-ipynb+json",
                        headers={"Content-Disposition": f'attachment; filename="course-{course.id[:8]}.ipynb"'})

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(web_dir / "index.html")

    application.mount("/static", StaticFiles(directory=str(web_dir)), name="static")
    return application


app = create_app()
