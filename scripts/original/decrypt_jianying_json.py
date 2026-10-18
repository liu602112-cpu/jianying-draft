## 用法
## python decrypt_jianying_json.py <input_file> <output_file>
## 示例：python decrypt_jianying_json.py resources/draft_content.json decrypted.json

"""
剪映（Jianying / CapCut CN）draft_content.json 解密/解析脚本

本脚本支持两类输入：
1) 明文 JSON：直接格式化输出；
2) 剪映草稿常见的 Base64 文本（可能带“keyIv 注入混淆”）：尝试还原并解密得到完整 JSON。

已验证的剪映 6+ 常见解密思路（本仓库 resources/draft_content.json 样本可用）：
- 草稿文件内容是一整段 Base64 字符串，但开头被“插入/混淆”了 48 个字符，称为 keyIv；
- 需要先按固定抽取规则把这 48 个字符提取出来（keyIv），剩余拼回得到真正的 newInput(Base64)；
- 解密时 keyIv 不是先 base64 解码成 bytes 再用，而是按 ASCII 直接切片：
    key = keyIv[:32].encode('utf-8')   # 32 bytes -> AES-256 key
    iv  = keyIv[32:48].encode('utf-8') # 16 bytes -> AES-GCM nonce/iv
  newInput(Base64) 解码后得到二进制：ciphertext || tag，其中 tag 在末尾 16 字节：
    ct  = data[:-16]
    tag = data[-16:]
    AES-GCM 解密：decrypt_and_verify(ct, tag) => 明文 JSON

命令行参数说明（常用）：
- -a / --analyze：仅做“Base64 解码后头部结构”的分析打印，不产出 JSON；用于排查其它版本/其它布局。
- --dump-keyiv：检测到 keyIv 注入格式时，把提取到的 keyIv（48 字符串）保存到指定文件。
- --dump-newinput：检测到 keyIv 注入格式时，把还原后的 newInput（真正 Base64）保存到指定文件。
- --dump-best：当所有尝试都失败时，把最“像 JSON”的候选明文二进制导出到 <output>.best.bin，便于二次分析。
- --debug：输出更多调试信息（例如某些解密分支失败原因）。

示例：
- 常规解密：python decrypt_jianying_json.py resources/draft_content.json decrypted.json
- 导出中间数据：python decrypt_jianying_json.py resources/draft_content.json out.json --dump-keyiv /tmp/keyIv.txt --dump-newinput /tmp/newInput.txt
- 仅分析头部：python decrypt_jianying_json.py -a resources/draft_content.json
"""

import base64
import json
import sys
import os
import binascii
import re
import argparse
from dataclasses import dataclass
from Crypto.Cipher import AES

# 尝试导入加密库
try:
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
except ImportError:
    try:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util import Counter
    except ImportError:
        print("错误: 未安装 'pycryptodome' 或 'pycryptodomex'。")
        print("请运行: pip install pycryptodomex")
        sys.exit(1)

_B64_RE = re.compile(rb"^[A-Za-z0-9+/=\s]+$")

_JY_KEYIV_PATTERN_N_BETWEEN = [3, 9, 9, 3, 3, 8, 3, 6, 9, 6, 24]

def _is_json_start(data: bytes) -> bool:
    """检查数据是否看起来像 JSON（忽略 BOM/空白，以 { 或 [ 开头）。"""
    try:
        text = data.decode("utf-8")
        text = text.lstrip("\ufeff").lstrip()
        return text.startswith("{") or text.startswith("[")
    except Exception:
        return False

def _looks_like_base64_text(data: bytes) -> bool:
    if not data:
        return False
    if not _B64_RE.match(data):
        return False
    # 去掉空白后，长度至少要能被 4 整除（严格 Base64）
    compact = re.sub(rb"\s+", b"", data)
    return len(compact) % 4 == 0 and len(compact) >= 16

def _maybe_base64_decode(data: bytes):
    """如果 data 看起来像 Base64 文本，则返回解码后的 bytes；否则返回 None。"""
    if not _looks_like_base64_text(data):
        return None
    compact = re.sub(rb"\s+", b"", data)
    try:
        return base64.b64decode(compact, validate=True)
    except Exception:
        return None

def _jianying_deinterleave_keyiv(raw_b64_text: bytes):
    """
    剪映 6+ 草稿文件常见的“keyIv 注入”形式：从开头按固定步长抽取 12 个 4-char 块（共 48 chars）
    组成 keyIv（48 字符串），剩余为 newInput（去除注入后的真正 Base64）。

    说明：
    - 在部分版本中，解密会直接使用 keyIv 的 ASCII 切片：keyIv[:32] 作为 key，keyIv[32:] 作为 nonce；
      keyIv 本身虽然看起来像 Base64，但不应假设一定需要先 base64 解码成 bytes 才能使用。

    返回 (keyIv_str, newInput_str) 或 None。
    """
    try:
        s = re.sub(rb"\s+", b"", raw_b64_text).decode("ascii")
    except Exception:
        return None
    if len(s) < 4 + 48:
        return None

    pos = 0
    key_chunks = []
    new_parts = []

    # 第一个 4 chars 来自 keyIv
    key_chunks.append(s[pos : pos + 4])
    pos += 4

    # 中间 11 段：N 个 newInput chars + 4 个 keyIv chars
    for n_len in _JY_KEYIV_PATTERN_N_BETWEEN:
        new_parts.append(s[pos : pos + n_len])
        pos += n_len
        key_chunks.append(s[pos : pos + 4])
        pos += 4

    new_parts.append(s[pos:])

    key_iv = "".join(key_chunks)
    new_input = "".join(new_parts)

    # 校验：keyIv 需要是 48 字符且看起来像 Base64 字符集；newInput 必须是可解码的 Base64
    if len(key_iv) != 48:
        return None
    if not _B64_RE.match(key_iv.encode("ascii")):
        return None
    try:
        base64.b64decode(new_input, validate=True)
    except Exception:
        return None

    return key_iv, new_input

def _extract_json_bytes(data: bytes):
    """从 bytes 中提取可解析的 JSON（允许前面有少量非 JSON 前缀）。"""
    # 1) 直接检查
    if _is_json_start(data):
        try:
            s = data.decode("utf-8")
            obj = json.loads(s)
            return obj
        except Exception:
            pass

    # 2) 在前 4096 字节内找 '{' / '[' 并尝试 raw_decode
    try:
        s = data.decode("utf-8")
    except Exception:
        return None
    scan_limit = min(len(s), 4096)
    positions = []
    for ch in ("{", "["):
        i = s.find(ch, 0, scan_limit)
        while i != -1:
            positions.append(i)
            i = s.find(ch, i + 1, scan_limit)
    for i in sorted(set(positions))[:200]:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[i:])
            return obj
        except Exception:
            continue
    return None

def _jsonish_score(sample: bytes) -> float:
    """用于在失败时挑一个“最像 JSON”的候选明文进行 dump。"""
    if not sample:
        return 0.0
    sample = sample[:65536]
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13)) / len(sample)
    json_chars = sum(1 for b in sample if b in b"{}[]\":,") / len(sample)
    zeros = sample.count(0) / len(sample)
    return printable + 2.0 * json_chars - 3.0 * zeros

@dataclass(frozen=True)
class Layout:
    name: str
    decoded: bytes
    header_len: int

    @property
    def header(self) -> bytes:
        return self.decoded[: self.header_len]

    @property
    def payload(self) -> bytes:
        return self.decoded[self.header_len :]

    def key32(self):
        h = self.header
        return h[16:48] if len(h) >= 48 else None

    def nonce12(self):
        h = self.header
        return h[48:60] if len(h) >= 60 else None

    def prefix16(self):
        h = self.header
        return h[:16] if len(h) >= 16 else None

def _iter_candidate_layouts(decoded: bytes):
    # 常见猜测：60/64 字节“明文头”；以及脚本原先的 258 字节头
    for name, header_len in [
        ("decoded:header60", 60),
        ("decoded:header64", 64),
        ("decoded:header258", 258),
    ]:
        if len(decoded) > header_len:
            yield Layout(name=name, decoded=decoded, header_len=header_len)

def _iter_plaintexts(layout: Layout):
    key = layout.key32()
    nonce12 = layout.nonce12()
    prefix16 = layout.prefix16() or b""
    payload = layout.payload

    if not key or not nonce12 or not payload:
        return

    # 1) AEAD: AES-GCM / AES-EAX / AES-SIV（多种 tag 位置 + AAD）
    aads = [
        ("aad:none", None),
        ("aad:prefix16", prefix16),
        ("aad:header", layout.header),
    ]
    splits = []
    if len(payload) >= 16:
        splits.append(("tag:tail16", payload[:-16], payload[-16:]))
        splits.append(("tag:head16", payload[16:], payload[:16]))

    for mode_name, mode in [("AES-GCM", AES.MODE_GCM), ("AES-EAX", AES.MODE_EAX)]:
        for split_name, ct, tag in splits:
            for aad_name, aad in aads:
                # verify
                try:
                    c = AES.new(key, mode, nonce=nonce12)
                    if aad is not None:
                        c.update(aad)
                    pt = c.decrypt_and_verify(ct, tag)
                    yield f"{layout.name} {mode_name} verify {split_name} {aad_name}", pt
                except Exception:
                    pass
                # force (忽略 tag)
                try:
                    c = AES.new(key, mode, nonce=nonce12)
                    if aad is not None:
                        c.update(aad)
                    pt = c.decrypt(ct)
                    yield f"{layout.name} {mode_name} force {split_name} {aad_name}", pt
                except Exception:
                    pass

    # SIV 需要 16/32 字节 nonce，且语义不同；这里尝试把 prefix16 作为 nonce
    try:
        c = AES.new(key, AES.MODE_SIV, nonce=prefix16[:16])
        c.update(layout.header)
        if len(payload) >= 16:
            pt = c.decrypt_and_verify(payload[:-16], payload[-16:])
            yield f"{layout.name} AES-SIV verify tag:tail16 aad:header", pt
    except Exception:
        pass

    # 2) 流模式：AES-CTR（几种常见 counter 组合）
    # 2.1 nonce12 + 32-bit counter（init 0/1/2/3）
    for init in (0, 1, 2, 3):
        try:
            ctr = Counter.new(32, prefix=nonce12, initial_value=init)
            c = AES.new(key, AES.MODE_CTR, counter=ctr)
            pt = c.decrypt(payload)
            yield f"{layout.name} AES-CTR nonce12 init={init}", pt
        except Exception:
            pass

    # 2.2 16-byte counter block = header[48:64]（若 header 足够长）
    if len(layout.header) >= 64:
        iv16 = layout.header[48:64]
        try:
            ctr = Counter.new(128, initial_value=int.from_bytes(iv16, "big"))
            c = AES.new(key, AES.MODE_CTR, counter=ctr)
            pt = c.decrypt(payload)
            yield f"{layout.name} AES-CTR counter=header[48:64]", pt
        except Exception:
            pass

    # 3) ChaCha20（非 AEAD）——如果库可用就试试
    try:
        from Crypto.Cipher import ChaCha20  # type: ignore
    except Exception:
        ChaCha20 = None
    if ChaCha20 is not None:
        for nonce_name, nonce in [("nonce12", nonce12), ("prefix12", prefix16[:12])]:
            try:
                c = ChaCha20.new(key=key, nonce=nonce)
                pt = c.decrypt(payload)
                yield f"{layout.name} ChaCha20 {nonce_name}", pt
            except Exception:
                pass

def decrypt_file(
    input_path: str,
    output_path: str,
    debug: bool = False,
    dump_best: bool = False,
    dump_newinput_path: str | None = None,
    dump_keyiv_path: str | None = None,
):
    if not os.path.exists(input_path):
        print(f"错误: 文件 '{input_path}' 不存在")
        return 1

    content = open(input_path, "rb").read()

    # 0) 已经是 JSON？
    if _is_json_start(content):
        try:
            obj = json.loads(content.decode("utf-8"))
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            print(f"✅ 输入文件本身就是 JSON，已保存: {output_path}")
            return 0
        except Exception:
            pass

    # 1) 读取为 Base64 文本（剪映 6+ 常见）。优先尝试“keyIv 注入”反混淆，再解码。
    if not _looks_like_base64_text(content):
        print("⚠️ 输入不是可识别的 Base64 文本，也不是 JSON。")
        print("当前脚本仅针对：明文 JSON 或 Base64 封装的二进制草稿。")
        return 2
    compact_b64 = re.sub(rb"\s+", b"", content)

    key_iv_str = None
    new_input_str = None
    deinterleaved = _jianying_deinterleave_keyiv(compact_b64)
    if deinterleaved is not None:
        key_iv_str, compact_str = deinterleaved
        new_input_str = compact_str
        compact_b64 = compact_str.encode("ascii")
        print("检测到剪映 6+ keyIv 注入格式：已提取 keyIv 并还原 newInput。")
        if debug:
            print(f"keyIv(base64): {key_iv_str}")
    else:
        try:
            new_input_str = compact_b64.decode("ascii")
        except Exception:
            new_input_str = None

    if dump_keyiv_path and key_iv_str:
        with open(dump_keyiv_path, "w", encoding="utf-8") as f:
            f.write(key_iv_str)
        print(f"已保存 keyIv(base64): {dump_keyiv_path}")

    if dump_newinput_path and new_input_str:
        with open(dump_newinput_path, "w", encoding="utf-8") as f:
            f.write(new_input_str)
        print(f"已保存 newInput(Base64): {dump_newinput_path}")

    decoded = _maybe_base64_decode(compact_b64)
    if decoded is None:
        print("Base64 解码失败：请确认输入文件内容确实为 Base64。")
        return 2

    print(f"Base64 解码成功：{len(compact_b64)} bytes -> {len(decoded)} bytes")

    # 2) 剪映 6+ 的常见方案：keyIv 并不是 Base64(bytes)，而是“ASCII 字符串拼接”
    #    - key = keyIv[0:32].encode()   (32 bytes -> AES-256)
    #    - iv  = keyIv[32:48].encode()  (16 bytes nonce)
    #    - newInput(Base64) 解码得到：ciphertext || tag(16)
    if key_iv_str and len(key_iv_str) == 48:
        try:
            jy_key = key_iv_str[:32].encode("utf-8")
            jy_nonce = key_iv_str[32:].encode("utf-8")
            if len(jy_key) == 32 and len(jy_nonce) == 16 and len(decoded) > 16:
                ct, tag = decoded[:-16], decoded[-16:]
                c = AES.new(jy_key, AES.MODE_GCM, nonce=jy_nonce)
                pt = c.decrypt_and_verify(ct, tag)
                obj = _extract_json_bytes(pt)
                if obj is not None:
                    with open(output_path, "w", encoding="utf-8") as f:
                        json.dump(obj, f, ensure_ascii=False, indent=2)
                    print("-" * 30)
                    print("✅ 解密/解析成功！")
                    print("使用的方法: 剪映6+ keyIv(ASCII) + AES-GCM(tag:tail16, aad:none)")
                    print(f"文件已保存: {output_path}")
                    return 0
        except Exception as e:
            if debug:
                print(f"[keyIv(ASCII) AES-GCM] 失败: {e}")

    best = None  # (score, name, plaintext)

    for layout in _iter_candidate_layouts(decoded):
        key = layout.key32()
        nonce12 = layout.nonce12()
        if key and nonce12:
            print(f"[{layout.name}] key={binascii.hexlify(key).decode()} nonce={binascii.hexlify(nonce12).decode()} payload_len={len(layout.payload)}")
        else:
            print(f"[{layout.name}] 头部不足以提取 key/nonce，跳过")
            continue

        for method_name, pt in _iter_plaintexts(layout, debug=debug):
            obj = _extract_json_bytes(pt)
            if obj is not None:
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=2)
                print("-" * 30)
                print("✅ 解密/解析成功！")
                print(f"使用的方法: {method_name}")
                print(f"文件已保存: {output_path}")
                return 0

            sc = _jsonish_score(pt)
            if best is None or sc > best[0]:
                best = (sc, method_name, pt)

    print("-" * 30)
    print("❌ 未能从任何候选算法中解析出 JSON。")
    print("说明：当前样本很可能不是简单的 AES-GCM/CTR/EAX 方案，或 key/nonce 并不在明文头部。")
    print("建议：提供草稿目录中的其它文件（例如 draft_meta_info.json）以便进一步推断密钥/算法。")

    if dump_best and best is not None:
        sc, method_name, pt = best
        dump_path = output_path + ".best.bin"
        with open(dump_path, "wb") as f:
            f.write(pt)
        print(f"已导出最“像 JSON”的候选明文（score={sc:.3f}）: {dump_path}")
        print(f"对应方法: {method_name}")

    return 3

def analyze_header(input_path: str):
    if not os.path.exists(input_path):
        print(f"错误: 文件 '{input_path}' 不存在")
        return 1

    content = open(input_path, "rb").read()
    if not _looks_like_base64_text(content):
        print("无法进行 Header 分析：输入不是 Base64 文本。")
        return 2

    compact_b64 = re.sub(rb"\s+", b"", content)
    deinterleaved = _jianying_deinterleave_keyiv(compact_b64)
    if deinterleaved is not None:
        key_iv_str, compact_str = deinterleaved
        compact_b64 = compact_str.encode("ascii")
        print("检测到剪映 6+ keyIv 注入格式：已提取 keyIv 并还原 newInput。")
        print(f"keyIv(base64): {key_iv_str}")
        try:
            print(f"keyIv(base64->bytes，仅用于分析): {base64.b64decode(key_iv_str, validate=True).hex()}")
        except Exception:
            pass
        if len(key_iv_str) == 48:
            print(f"keyIv(ascii key32): {key_iv_str[:32]}")
            print(f"keyIv(ascii iv16):  {key_iv_str[32:]}")

    decoded = _maybe_base64_decode(compact_b64)
    if decoded is None:
        print("无法进行 Header 分析：Base64 解码失败。")
        return 2

    print(f"Base64 解码成功：{len(compact_b64)} bytes -> {len(decoded)} bytes")
    for layout in _iter_candidate_layouts(decoded):
        h = layout.header
        print(f"\n--- {layout.name} ---")
        print(f"header_len={layout.header_len} payload_len={len(layout.payload)}")
        if len(h) >= 16:
            print(f"prefix16: {binascii.hexlify(h[:16]).decode()}")
        if len(h) >= 48:
            print(f"key32@16: {binascii.hexlify(h[16:48]).decode()}")
        if len(h) >= 60:
            print(f"nonce12@48: {binascii.hexlify(h[48:60]).decode()}")
        if len(h) >= 64:
            print(f"iv16@48: {binascii.hexlify(h[48:64]).decode()}")
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="尝试解密/解析剪映 draft_content.json（支持明文 JSON 或 Base64 封装的二进制草稿）")
    parser.add_argument("input", help="输入文件路径（通常为 draft_content.json）")
    parser.add_argument("output", nargs="?", default="decrypted.json", help="输出 JSON 文件路径")
    parser.add_argument("-a", "--analyze", action="store_true", help="仅分析 Base64 解码后的头部结构")
    parser.add_argument("--debug", action="store_true", help="输出更多调试信息")
    parser.add_argument("--dump-best", action="store_true", help="失败时导出最像 JSON 的候选明文到 <output>.best.bin")
    parser.add_argument("--dump-newinput", help="若检测到 keyIv 注入格式，将还原后的 newInput(Base64) 保存到指定路径")
    parser.add_argument("--dump-keyiv", help="若检测到 keyIv 注入格式，将提取到的 keyIv(Base64) 保存到指定路径")
    args = parser.parse_args()

    if args.analyze:
        raise SystemExit(analyze_header(args.input))
    raise SystemExit(
        decrypt_file(
            args.input,
            args.output,
            debug=args.debug,
            dump_best=args.dump_best,
            dump_newinput_path=args.dump_newinput,
            dump_keyiv_path=args.dump_keyiv,
        )
    )
