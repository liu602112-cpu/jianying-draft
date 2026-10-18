"""Report Windows installation capabilities without changing a Jianying draft."""
import argparse
import importlib.util
import json
import subprocess
from pathlib import Path
import jy_runtime as R


def main():
    parser = argparse.ArgumentParser(description="Windows 剪映工具环境检查，只读")
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    checks = {"platform": __import__("sys").platform,
              "draft_roots": [{"path": path.as_posix(), "exists": path.is_dir()}
                              for path in R.draft_roots()],
              "crypto": importlib.util.find_spec("Crypto") is not None,
              "pillow": importlib.util.find_spec("PIL") is not None}
    for name in ("ffmpeg", "ffprobe", "whisper-cli"):
        try:
            executable = R.tool(name)
            result = subprocess.run([executable, "--help" if name == "whisper-cli" else "-version"],
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=20)
            checks[name] = {"path": executable, "runs": result.returncode == 0,
                            "version": (result.stdout or result.stderr).splitlines()[0]}
        except (OSError, subprocess.SubprocessError, IndexError) as error:
            checks[name] = {"runs": False, "error": str(error)}
    model = next((path for path in R.whisper_models() if path.is_file()), None)
    checks["whisper_model"] = {"path": str(model) if model else None,
                               "bytes": model.stat().st_size if model else 0}
    checks["fonts"] = {"keyword_chinese": R.font("猴尊宋体.ttf", "simsun.ttc"),
                       "keyword_english": R.font("Cherolina.otf", "arial.ttf"),
                       "subtitle": R.font("SourceHanSansCN-Regular.otf", "msyh.ttc")}
    checks["original_fonts_available"] = {
        "猴尊宋体": Path(checks["fonts"]["keyword_chinese"]).name == "猴尊宋体.ttf",
        "Cherolina": Path(checks["fonts"]["keyword_english"]).name == "Cherolina.otf"}
    checks["blur_cache"] = R.effect_path("7399495140399746304")
    checks["core_ready"] = checks["crypto"] and checks["ffmpeg"].get("runs", False) and checks["ffprobe"].get("runs", False)
    checks["speech_check_ready"] = checks["whisper-cli"].get("runs", False) and checks["whisper_model"]["bytes"] > 100_000_000
    checks["verification_scope"] = "工具运行状态；不代表真实工程已打开或视觉效果已验证"
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["core_ready"] and checks["speech_check_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
