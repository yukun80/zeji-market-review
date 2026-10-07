"""Read only the explicitly supported project-local credential settings."""
import json
import os
from .core import ROOT


def load_local_config(path=None):
    path = path if path is not None else ROOT / 'local-config.json'
    if not path.exists():
        return
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise ValueError('local-config.json无法读取，请检查文件格式；内容不会显示') from None
    if not isinstance(value, dict) or set(value) - {'TUSHARE_TOKEN', 'SEC_USER_AGENT'}:
        raise ValueError('local-config.json仅支持TUSHARE_TOKEN和SEC_USER_AGENT')
    if any(not isinstance(v, str) or '\n' in v or '\r' in v for v in value.values()):
        raise ValueError('local-config.json中的配置须为单行文字')
    for key, item in value.items():
        if item.strip() and not os.environ.get(key, '').strip():
            os.environ[key] = item.strip()
