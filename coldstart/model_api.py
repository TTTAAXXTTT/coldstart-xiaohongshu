"""Chat Completions provider templates; no credentials or network side effects."""
from urllib.parse import urlsplit

PRESETS = {
    'ark': {'name': '火山方舟 / 豆包', 'base_url': 'https://ark.cn-beijing.volces.com/api/v3', 'model': 'doubao-seed-2-1-lite-260915', 'key_env': 'ARK_API_KEY'},
    'deepseek': {'name': 'DeepSeek', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash', 'key_env': 'DEEPSEEK_API_KEY'},
    'qwen': {'name': '阿里云百炼 / 千问', 'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'model': 'qwen-plus', 'key_env': 'DASHSCOPE_API_KEY'},
    'openai': {'name': 'OpenAI', 'base_url': 'https://api.openai.com/v1', 'model': '', 'key_env': 'OPENAI_API_KEY'},
    'custom': {'name': '其他 Chat Completions 兼容接口', 'base_url': '', 'model': '', 'key_env': ''},
}


def public_presets():
    return [{'id': key, **{k: v for k, v in value.items() if k != 'key_env'}} for key, value in PRESETS.items()]


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or '').lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)


def is_ark(url):
    return urlsplit(url).hostname == 'ark.cn-beijing.volces.com'


def provider_id(url):
    host = urlsplit(url).hostname
    if host and (host.endswith('.maas.aliyuncs.com') or host.endswith('.dashscope.aliyuncs.com')):
        return 'qwen'
    return next((key for key, value in PRESETS.items() if key != 'custom' and urlsplit(value['base_url']).hostname == host), 'custom')


def completion_url(base):
    if base.endswith('/chat/completions'):
        return base
    parsed = urlsplit(base)
    suffix = '/chat/completions' if parsed.path.strip('/') or parsed.hostname == 'api.deepseek.com' else '/v1/chat/completions'
    return base + suffix


def request_body(base, model, system, content, max_tokens):
    host = urlsplit(base).hostname
    body = {'model': model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}]}
    if host == 'api.openai.com':
        if max_tokens is not None and max_tokens>=0:body['max_completion_tokens'] = max_tokens
        # Let the selected OpenAI model use its supported sampling defaults.
    else:
        body.update(temperature=0.4)
        if max_tokens is not None and max_tokens>=0:body['max_tokens'] = max_tokens
    if is_ark(base) and model.startswith('doubao-seed-2-1-'):
        body.update(thinking={'type': 'disabled'}, response_format={'type': 'json_object'})
    if host == 'api.deepseek.com' and model.startswith('deepseek-'):
        body['thinking'] = {'type': 'disabled'}
    return body
