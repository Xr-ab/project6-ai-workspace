"""知识库文档接口的请求 / 响应模型（Phase 2）。

为什么响应模型不含 file_path / checksum：
    file_path 是服务器磁盘路径，暴露出去等于告诉别人内部目录结构；
    checksum 是增量索引去重用的内部字段，前端用不上。
    响应模型是"对外契约"——这里没声明的字段就不会出现在 JSON 里。
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentOut(BaseModel):
    """文档摘要（上传响应 / 列表共用）。

    status 取值：uploaded / parsing / chunking / embedding / ready / failed
    status=failed 时 error_message 有值，前端据此显示失败原因。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    file_type: str
    size_bytes: int
    status: str
    error_message: str | None
    chunk_count: int
    created_at: datetime
