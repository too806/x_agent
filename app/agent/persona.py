from pathlib import Path

import yaml
from pydantic import BaseModel

from app.config import settings


class Persona(BaseModel):
    name: str
    description: str
    tone: str
    style: list[str]
    avoid: list[str]
    max_length: int


def load_persona(path: str | None = None) -> Persona:
    file_path = Path(path or settings.persona_path)
    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    return Persona(**data)
