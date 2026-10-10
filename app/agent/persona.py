from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.config import settings


class Voice(BaseModel):
    """阅读契约：这份内容「怎么说话」。

    与 `StyleRecipe` 的分工是刻意的——recipe 说「写成什么形状」（结构 / 长度 /
    密度，来自语料观察），voice 说「用一个什么姿态说话」（语气 / 节奏 / 张力）。
    两者冲突时以 voice 为准：形状可以让步，声音不让。

    每一项都是可选的（默认空列表）。所以给契约加一条新条款，只需要改
    `config/persona.yaml`，不必动任何调用方。
    """

    structure: list[str] = Field(default_factory=list)
    hook_rules: list[str] = Field(default_factory=list)
    rhythm_rules: list[str] = Field(default_factory=list)
    plain_language: list[str] = Field(default_factory=list)
    allowed_devices: list[str] = Field(default_factory=list)
    #: 怎么收尾。「不要求每条都提问」必须能被写进契约里——否则模型会把
    #: `allowed_devices` 里的互动读成"每条结尾都加一个你怎么看"。
    ending_rules: list[str] = Field(default_factory=list)
    fabrication_red_lines: list[str] = Field(default_factory=list)


class Persona(BaseModel):
    name: str
    description: str
    tone: str
    style: list[str]
    #: 正文的**写作目标上限**（字符）。它同时是 guardrail 与发布路径实际执行
    #: 那一条线（见 `nodes/guardrail.py`、`services/publish.py`），所以改它
    #: 会真的改变能发出去的正文长度。
    max_length: int
    #: 推荐生成区间的下沿。与 `max_length` **不是同类**：上限是校验，
    #: 下沿只是写作偏好——短于它不构成任何拒绝理由，也不该变成新的门禁。
    #:
    #: `None`（老配置）时 Writer prompt 只说上限，不报区间；渲染成 `0`
    #: 会让模型读到"最少 0 字"这种没有信息量的话。
    recommended_min_length: int | None = None
    #: 阅读契约。`None` 表示这份 persona 没有声明声音（旧配置、或只想换上限）。
    #: 刻意**没有**声明的字段就不能靠默认值悄悄补一套——渲染层遇到 `None` 就跳过，
    #: 于是「没有契约」和「有契约」在 prompt 里长得不一样，能被看出来。
    voice: Voice | None = None


def load_persona(path: str | None = None) -> Persona:
    file_path = Path(path or settings.persona_path)
    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    return Persona(**data)
