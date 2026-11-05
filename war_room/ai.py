"""用本機已登入的 Claude Code 或 Codex CLI 回答提示：模型跑在使用者自己的訂閱上（同 FinLab Desktop）。"""
import json, os, re, shutil, subprocess, tempfile


def engine():
    return next((e for e in ('claude', 'codex') if shutil.which(e)), None)


def ask(system, text, timeout=300):
    e = engine()
    with tempfile.TemporaryDirectory() as d:
        if e == 'claude':
            cmd = ['claude', '-p', '--model', 'haiku', '--tools', '', '--system-prompt', system, '--output-format', 'json']
            r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=timeout, cwd=d)
            return json.loads(r.stdout)['result']
        if e == 'codex':
            out = os.path.join(d, 'out.txt')
            cmd = ['codex', 'exec', '--skip-git-repo-check', '--sandbox', 'read-only', '-c', 'model_reasoning_effort="low"', '-o', out, '-']
            subprocess.run(cmd, input=f'{system}\n\n{text}', capture_output=True, text=True, timeout=timeout, cwd=d)
            return open(out).read()
    raise RuntimeError('這台電腦找不到 Claude Code 或 Codex CLI')


def ask_json(system, text, timeout=300):
    reply = ask(system, text, timeout)
    return json.loads(re.search(r'\{.*\}', reply, re.S).group(0))
