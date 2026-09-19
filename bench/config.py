from __future__ import annotations
import os
from pathlib import Path
from typing import Any
import yaml
from pydantic import BaseModel, Field


class ANSCfg(BaseModel):
    search_base: str
    transparency_base: str
    rate_limit_per_minute: int = 100
    timeout_s: int = 15


class TargetCfg(BaseModel):
    host: str
    ans_id: str = ""
    transport: str = "a2a"
    endpoint: str = ""
    timeout_s: int = 30


class TransportCfg(BaseModel):
    a2a_method: str = "message/send"
    mcp_tool: str = ""


class GeneratorCfg(BaseModel):
    enabled: bool = True
    model: str = "claude-sonnet-4-6"
    max_generated: int = 30


class ReportCfg(BaseModel):
    out_dir: str = "out"
    weights: dict[str, float] = Field(default_factory=lambda: {"oracle": .7, "selfclaim": .25, "schema": .2, "quality": .1})


class EmitCfg(BaseModel):
    enabled: bool = False
    import_url: str = ""
    admin_key: str = ""


class Config(BaseModel):
    ans: ANSCfg
    target: TargetCfg
    transport: TransportCfg = TransportCfg()
    generator: GeneratorCfg = GeneratorCfg()
    report: ReportCfg = ReportCfg()
    emit: EmitCfg = EmitCfg()
    suite: str = "generated"   # generated | regression | both  (CLI --suite overrides)
    live: bool = False   # set by CLI; False => mock network everywhere


def load(path: str | Path = "config.yaml", **overrides: Any) -> Config:
    data = yaml.safe_load(Path(path).read_text())
    cfg = Config(**data)
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, v)
    if os.getenv("ANS_TARGET_HOST"):
        cfg.target.host = os.environ["ANS_TARGET_HOST"]
    return cfg
