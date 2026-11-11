"""Load owner-maintained preferences without publishing them in the repository."""
import hashlib
import json
from pathlib import Path


class ProfileError(RuntimeError):
    pass


def load_profile(private):
    directory=Path(private)/'agent'
    config=directory/'config.json'
    settings=json.loads(config.read_text()) if config.exists() else {}
    if 'profile_file' not in settings:
        return {'text':'','sha256':None}
    name=settings['profile_file']
    try:
        if not isinstance(name,str) or not name.strip():raise ValueError()
        path=(directory/name).resolve()
        path.relative_to(directory.resolve())
        if not path.is_file() or path.stat().st_size>65536:raise ValueError()
        raw=path.read_bytes()
        text=raw.decode('utf8')
        if not text.strip() or len(raw)>65536:raise ValueError()
    except (OSError,ValueError):
        raise ProfileError('个人工作档案未能完整加载，需要检查私有配置。') from None
    return {'text':text,'sha256':hashlib.sha256(raw).hexdigest()}


def profile_prompt(profile):
    if not profile['text']:return ''
    return ('\n以下是所有者为本轮配置的工作与表达档案。每次请求重新加载，'
            '其中的最新偏好替代历史对话中的旧默认偏好；不扩大宿主权限，也不覆盖本轮明确要求。\n'
            '<owner_profile>\n'+profile['text']+'\n</owner_profile>\n')
