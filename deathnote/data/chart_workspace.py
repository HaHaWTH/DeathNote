"""Candidate staging with inherited Windows workspace permissions."""
from contextlib import contextmanager
from pathlib import Path
import secrets
import shutil


@contextmanager
def candidate_stage(parent):
    parent = Path(parent).resolve()
    stage = parent / ('.chart-stage-' + secrets.token_hex(12))
    stage.mkdir()
    try:
        yield stage
    finally:
        if not stage.resolve().is_relative_to(parent):
            raise ValueError('候选暂存目录超出构建目录')
        shutil.rmtree(stage)
