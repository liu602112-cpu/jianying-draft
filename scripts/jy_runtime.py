"""Windows portability helpers for Weiyu's Jianying tools.

Original project: https://github.com/sakuraoxo-clio/claude-skills
Windows adaptation: local Codex installation, 2026-10-02.
"""
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
VENDOR = SKILL / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def local_appdata():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))


def draft_roots():
    explicit = os.environ.get("JY_DRAFT_ROOT")
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    return [local_appdata() / app / "User Data/Projects/com.lveditor.draft"
            for app in ("JianyingPro", "CapCut")]


def tool(name):
    override = os.environ.get("JY_" + name.upper().replace("-CLI", ""))
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"工具路径不存在：{path}")
        return str(path)
    exe = name + (".exe" if os.name == "nt" else "")
    candidates = [VENDOR / "bin" / exe, VENDOR / "whisper" / "Release" / exe,
                  VENDOR / "whisper" / exe]
    if name == "whisper-cli" and (VENDOR / "whisper").is_dir():
        candidates.extend((VENDOR / "whisper").rglob(exe))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"缺少 {exe}；运行本安装包的 install.ps1 补齐依赖")


def whisper_models():
    override = os.environ.get("JY_WHISPER_MODEL")
    return ([Path(override).expanduser()] if override else []) + [
        VENDOR / "models/ggml-small.bin",
        Path.home() / ".cache/hyperframes/whisper/models/ggml-small.bin"]


def short_path(path):
    """Some native Whisper Windows builds reject Unicode data file paths."""
    path = Path(path).resolve()
    if str(path).isascii() or os.name != "nt":
        return path
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return Path(buffer.value) if 0 < size < len(buffer) else path


def whisper_cache():
    override = os.environ.get("JY_WHISPER_CACHE")
    candidates = [Path(override)] if override else [
        local_appdata() / "Codex/Jianying/whisper",
        Path(tempfile.gettempdir()) / "codex-jianying-whisper"]
    for path in candidates:
        path.mkdir(parents=True, exist_ok=True)
        compatible = short_path(path)
        if str(compatible).isascii():
            return compatible
    raise RuntimeError("Whisper 需要英文临时路径，请将 JY_WHISPER_CACHE 指向可写的英文目录")


def transcribe(wav, model, prompt="以下是普通话的句子。", threads=8):
    """Stage native CLI input/output under ASCII paths; keep source media intact."""
    cache = whisper_cache()
    compatible_model = short_path(model)
    if not str(compatible_model).isascii():
        source = Path(model).resolve()
        key = hashlib.sha256((str(source) + str(source.stat().st_size) + str(source.stat().st_mtime_ns)).encode()).hexdigest()[:20]
        compatible_model = cache / ("model-" + key + ".bin")
        if not compatible_model.is_file():
            try:
                os.link(source, compatible_model)
            except OSError:
                shutil.copy2(source, compatible_model)
    with tempfile.TemporaryDirectory(prefix="check-", dir=cache) as directory:
        scratch = Path(directory)
        audio = scratch / "audio.wav"
        shutil.copy2(wav, audio)
        output = scratch / "result"
        result = subprocess.run([tool("whisper-cli"), "-m", str(compatible_model), "-l", "zh",
                                 "-t", str(threads), "-np", "--prompt", prompt,
                                 "-f", str(audio), "-otxt", "-of", str(output)],
                                capture_output=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(f"Whisper 识别失败（{result.returncode}）：{result.stderr[-2000:]}")
        return output.with_suffix(".txt").read_text(encoding="utf-8-sig")


def font(name, fallback="msyh.ttc"):
    roots = [local_appdata() / "Microsoft/Windows/Fonts",
             Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    for candidate_name in (name, fallback):
        for root in roots:
            candidate = root / candidate_name
            if candidate.is_file():
                return candidate.as_posix()
    raise FileNotFoundError(f"找不到字体 {name} 或替代字体 {fallback}")


def effect_path(resource_id):
    for app in ("JianyingPro", "CapCut"):
        directory = local_appdata() / app / "User Data/Cache/effect" / str(resource_id)
        try:
            if directory.is_dir():
                children = sorted(p for p in directory.iterdir() if p.is_dir())
                return (children[0] if children else directory).as_posix()
        except OSError:
            continue
    return ""


def adapt_paths(value):
    """Remap preset paths structurally, including JSON embedded in content fields."""
    if isinstance(value, dict):
        out = {key: adapt_paths(item) for key, item in value.items()}
        if out.get("font_path") and str(out["font_path"]).startswith("C:"):
            out["font_resource_id"] = ""
        if isinstance(out.get("path"), str) and out["path"].endswith((".ttf", ".otf", ".ttc")) and "id" in out:
            out["id"] = ""
        return out
    if isinstance(value, list):
        return [adapt_paths(item) for item in value]
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(adapt_paths(json.loads(value)), ensure_ascii=False,
                              separators=(",", ":"))
        except ValueError:
            pass
    normalized = value.replace("\\", "/")
    if "/Library/Fonts/" in normalized:
        preferred = normalized.rsplit("/", 1)[-1]
        return font(preferred, "arial.ttf" if "Cherolina" in preferred else "simsun.ttc")
    if "/Movies/JianyingPro/User Data/Cache/effect/" in normalized:
        suffix = normalized.split("/Cache/effect/", 1)[1]
        resource_id = suffix.split("/", 1)[0]
        if normalized.endswith((".ttf", ".otf", ".ttc")):
            directory = local_appdata() / "JianyingPro/User Data/Cache/effect" / resource_id
            if directory.is_dir():
                try:
                    matches = list(directory.rglob(normalized.rsplit("/", 1)[-1]))
                    if matches:
                        return matches[0].as_posix()
                except OSError:
                    pass
            return font("SourceHanSansCN-Regular.otf", "msyh.ttc")
        return effect_path(resource_id)
    if normalized.startswith("~/"):
        return (Path.home() / normalized[2:]).as_posix()
    return value


def safe_child(root, name):
    root = Path(root).resolve()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if (not name or name in (".", "..") or name.endswith((".", " "))
            or name.split(".")[0].upper() in reserved or any(ch in name for ch in '<>:"/\\|?*')):
        raise ValueError(f"草稿名称含 Windows 不支持的字符：{name}")
    destination = (root / name).resolve()
    if destination.parent != root:
        raise ValueError("目标草稿必须位于指定草稿目录内")
    return destination


def ensure_editor_closed(path):
    """Guard live project writes; sandbox fixtures outside live roots are allowed."""
    target = Path(path).resolve()
    live_roots = [local_appdata() / app / "User Data/Projects/com.lveditor.draft"
                  for app in ("JianyingPro", "CapCut")]
    if not any(target.is_relative_to(root.resolve()) for root in live_roots):
        return
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                encoding="utf-8", errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        if any('"' + name + '"' in result.stdout.lower()
               for name in ("jianyingpro.exe", "capcut.exe")):
            raise RuntimeError("剪映仍在运行，请先保存并退出剪映，再写入草稿。工具不会自动关闭剪映。")


def write_main_files(directory, info, main_name=None, raw=None):
    from jy import MAIN_FILES, decrypt_text, encrypt_text, resolve
    directory = Path(directory).resolve()
    ensure_editor_closed(directory)
    main = directory / main_name if main_name else resolve(str(directory))
    original = raw if raw is not None else main.read_text(encoding="utf-8-sig")
    plain = json.dumps(info, ensure_ascii=False, separators=(",", ":"))
    encrypted = None
    def encode_like(text):
        nonlocal encrypted
        if text.lstrip().startswith(("{", "[")):
            return plain
        if encrypted is None:
            encrypted = encrypt_text(plain)
        return encrypted
    roots = [directory, directory / "Timelines" / info["id"]]
    for root in roots:
        if not root.is_dir():
            continue
        for name in (*MAIN_FILES, *(name + ".bak" for name in MAIN_FILES), "template-2.tmp"):
            path = root / name
            if not path.is_file():
                continue
            previous = path.read_text(encoding="utf-8-sig", errors="ignore")
            if name == "template-2.tmp" and previous != original:
                continue
            temporary = path.with_name(path.name + ".jy_tmp_" + uuid.uuid4().hex)
            temporary.write_text(encode_like(previous), encoding="utf-8")
            os.replace(temporary, path)
    if not main.is_file():
        main.write_text(encode_like(original), encoding="utf-8")
    back = json.loads(decrypt_text(main.read_text(encoding="utf-8-sig")))
    if back != info:
        raise RuntimeError("草稿写回校验失败")
    return main


def analysis_cache():
    """Analysis-only cache, separate from the user's source drafts."""
    return Path(os.environ.get("JY_ANALYSIS_CACHE", str(local_appdata() / "Codex/Jianying/analysis")))
