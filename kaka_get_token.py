# -*- coding: utf-8 -*-
"""
绩效 tokenId7 取号助手（在你自己的电脑上运行）

用途：从本机【电脑版微信】里读出绩效登录态 tokenId7，自动复制到剪贴板，
      然后回到“咔咔 OA 助手”网页，在绩效那一栏粘贴保存即可。

前提：
  1. 先用电脑微信打开过一次绩效页（公众号里那个绩效/成绩）。
  2. 本机已装 cryptography：  pip install cryptography
运行：  python kaka_get_token.py
"""

import os
import sys
import json
import base64
import shutil
import sqlite3
import tempfile
import subprocess


def _dpapi_unprotect(data):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    bi = BLOB(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_char)))
    bo = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        raise OSError("CryptUnprotectData 失败")
    out = ctypes.string_at(bo.pbData, bo.cbData)
    ctypes.windll.kernel32.LocalFree(bo.pbData)
    return out


def _engine_aes_key(local_state_path):
    ls = json.load(open(local_state_path, encoding="utf-8"))
    return _dpapi_unprotect(base64.b64decode(ls["os_crypt"]["encrypted_key"])[5:])


def _decrypt_cookie(enc, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if enc[:3] in (b"v10", b"v11"):
        pt = AESGCM(key).decrypt(enc[3:15], enc[15:], None)
        try:
            return pt.decode("utf-8")
        except UnicodeDecodeError:
            return pt[32:].decode("utf-8", "replace")
    return _dpapi_unprotect(enc).decode("utf-8", "replace")


def _find_local_state(cookie_path):
    d = os.path.dirname(cookie_path)
    for _ in range(6):
        cand = os.path.join(d, "Local State")
        if os.path.isfile(cand):
            return cand
        d = os.path.dirname(d)
    return None


_SKIP_DIRS = {"msg", "filestorage", "backup", "video", "image", "img",
              "emoji", "filecache", "crashpad", "temp", "tmp"}


def _candidate_cookie_dbs(extra_roots=None):
    home = os.path.expanduser("~")
    prof = os.environ.get("USERPROFILE") or home
    bases = (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
             os.path.join(home, "AppData", "Roaming"),
             os.path.join(home, "AppData", "Local"))
    roots, direct = [], []
    for base in bases:
        if base:
            for brand in ("Tencent", "WeChat", "Weixin", "xwechat"):
                roots.append(os.path.join(base, brand))
    roots.append(os.path.join(prof, "Documents", "WeChat Files"))
    roots.append(os.path.join(prof, "Documents", "xwechat_files"))
    for r in (extra_roots or []):
        p = os.path.normpath(os.path.expandvars(os.path.expanduser(str(r).strip().strip('"'))))
        if os.path.isfile(p):
            direct.append(p)
        elif os.path.isdir(p):
            roots.append(p)
    dbs, seen = list(direct), set()
    for root in roots:
        root = os.path.normpath(root)
        if not os.path.isdir(root) or root in seen:
            continue
        seen.add(root)
        for dp, dn, fn in os.walk(root):
            if dp[len(root):].count(os.sep) > 9:
                dn[:] = []
                continue
            dn[:] = [d for d in dn if d.lower() not in _SKIP_DIRS]
            if "Cookies" in fn:
                dbs.append(os.path.join(dp, "Cookies"))
    dbs = list(dict.fromkeys(dbs))
    dbs.sort(key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0, reverse=True)
    return dbs


def _copy_db(db_path, tmp):
    """复制 Cookies 及 WAL/SHM 旁文件（微信运行时新 cookie 在 -wal 里）。"""
    dst = os.path.join(tmp, "Cookies")
    shutil.copy(db_path, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.copy(db_path + suffix, dst + suffix)
    return dst


def _query_token_enc(db_path):
    tmp = tempfile.mkdtemp()
    try:
        con = sqlite3.connect(_copy_db(db_path, tmp))
        try:
            row = con.execute(
                "SELECT encrypted_value FROM cookies "
                "WHERE host_key LIKE '%kk.xwtec%' AND name='tokenId7'").fetchone()
        finally:
            con.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_wechat_tokenid7(extra_roots=None):
    if sys.platform != "win32":
        return None
    dbs = _candidate_cookie_dbs(extra_roots)
    all_ls = []
    for db in dbs:
        ls = _find_local_state(db)
        if ls and ls not in all_ls:
            all_ls.append(ls)
    for db in dbs:
        enc = _query_token_enc(db)
        if not enc:
            continue
        near = _find_local_state(db)
        for ls in ([near] if near else []) + [x for x in all_ls if x != near]:
            try:
                val = _decrypt_cookie(enc, _engine_aes_key(ls))
                if val and val.strip():
                    return val.strip()
            except Exception:
                continue
    return None


def copy_to_clipboard(text):
    try:
        subprocess.run("clip", input=text.encode("utf-8"), shell=True, check=True)
        return True
    except Exception:
        return False


def _scan_db(db_path):
    """返回 (是否有tokenId7, 含xwtec的host列表, 总cookie数)。"""
    tmp = tempfile.mkdtemp()
    try:
        con = sqlite3.connect(_copy_db(db_path, tmp))
        try:
            xw = con.execute("SELECT DISTINCT host_key,name FROM cookies "
                             "WHERE host_key LIKE '%xwtec%'").fetchall()
            total = con.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
        finally:
            con.close()
        has = any(n == "tokenId7" for _, n in xw)
        return has, xw, total
    except Exception as e:
        return False, [("<打不开>", str(e)[:40])], -1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def diagnose(extra_roots=None):
    """打印诊断报告：找到了哪些 Cookies 库、各自有没有 xwtec 的 cookie。"""
    dbs = _candidate_cookie_dbs(extra_roots)
    print("\n==== 诊断：本机发现的 Cookies 库 ====")
    if not dbs:
        print("  未发现任何 Cookies 库。微信可能装在非默认位置。")
        print("  请手动指定目录再试，例如：python kaka_get_token.py \"D:\\你的\\微信目录\"")
        return
    for db in dbs:
        has, xw, total = _scan_db(db)
        tag = "★有tokenId7" if has else ("·有xwtec" if xw else "  无关")
        print(f"  [{tag}] (共{total}条) {db}")
        for host, name in xw:
            print(f"        - {host}  {name}")
    print("\n把以上内容发给作者，即可定位你这台机器微信存登录态的位置。")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("缺少依赖，请先运行：pip install cryptography")
        return
    # 可选：命令行传入微信目录（自动找不到时用），如
    #   python kaka_get_token.py "%APPDATA%\Tencent"
    extra = sys.argv[1:] or None
    try:
        tk = read_wechat_tokenid7(extra)
    except Exception as e:
        print("读取失败：", e)
        return
    if not tk:
        print("× 没在本机微信里找到绩效登录态。")
        print("  1) 先用【电脑版微信】打开一次绩效页（公众号里那个成绩/绩效），再运行本助手；")
        print("  2) 仍不行可手动指定目录：python kaka_get_token.py \"%APPDATA%\\Tencent\"")
        diagnose(extra)
        return
    print("\n你的绩效 tokenId7：\n")
    print("   " + tk + "\n")
    if copy_to_clipboard(tk):
        print("✓ 已复制到剪贴板。回到“咔咔 OA 助手”网页，在绩效栏粘贴保存即可。")
    else:
        print("（复制剪贴板失败，请手动复制上面这串，粘贴到网页绩效栏。）")


if __name__ == "__main__":
    main()
