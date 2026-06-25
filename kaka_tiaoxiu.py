# -*- coding: utf-8 -*-
"""
咔咔 OA 助手（全自动 · 本地网页版）

只靠 PC 微信里的绩效登录态(tokenId7) 就能跑通一切，不需要 jwt、不需要扫码：
  - 启动后自动从本机微信读出 tokenId7
  - 加班 / 调休：kk.xwtec.net/vcardoah5/oa/*   （tokenId7 鉴权）
  - 绩效：       kk.xwtec.net/vcardh5/pref/*    （tokenId7 鉴权）
  - 近三个月结算 + 剩余可调休 + 七天内到期作废提醒（加班满三个月作废）

运行：  python kaka_tiaoxiu.py
给同事用：各自在自己电脑上运行（读各自微信），即各自全自动。
"""

import os
import sys
import re
import json
import time
import base64
import socket
import shutil
import sqlite3
import secrets
import calendar
import tempfile
import threading
import webbrowser
from datetime import datetime, date, timedelta

import requests
from flask import Flask, request, jsonify, Response

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
BASE_NET = "https://kk.xwtec.net"
PORT = 5666
MONTHS = 3                          # 统计窗口 & 加班作废周期（月）
EXPIRE_SOON_DAYS = 7                # “即将到期”提醒阈值（天）
SESSIONS_FILE = "kaka_sessions.json"

UA_WECHAT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI "
             "MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) "
             "UnifiedPCWindowsWechat(0xf2541923) XWEB/19841 Flue")

# 加班=JBSQ(JB)  调休=JBTX(TX)
OVERTIME_PREFIXES = ("REQ_JBSQ",)
TIAOXIU_PREFIXES = ("REQ_JBTX",)
FLAG_DONE = 4
FLAG_LABELS = {4: "已通过", 2: "已撤回"}

# ----------------------------------------------------------------------------
# 多会话存储：sid -> ctx（每个浏览器一份；各自的 tokenId7）
# ----------------------------------------------------------------------------
SESSIONS = {}
LOCK = threading.Lock()


def new_ctx():
    return {"tokenId7": None, "name": None}


def load_sessions():
    if not os.path.exists(SESSIONS_FILE):
        return
    try:
        data = json.load(open(SESSIONS_FILE, encoding="utf-8"))
        for sid, c in data.items():
            ctx = new_ctx()
            ctx.update({k: c.get(k) for k in ("tokenId7", "name")})
            SESSIONS[sid] = ctx
    except Exception:
        pass


def save_sessions():
    with LOCK:
        dump = {sid: {k: c.get(k) for k in ("tokenId7", "name")}
                for sid, c in SESSIONS.items() if c.get("tokenId7")}
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)


def ensure_ctx():
    sid = request.cookies.get("sid")
    if sid and sid in SESSIONS:
        return SESSIONS[sid], sid, False
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = new_ctx()
    return SESSIONS[sid], sid, True


def current_ctx():
    sid = request.cookies.get("sid")
    return SESSIONS.get(sid) if sid else None


# ----------------------------------------------------------------------------
# 日期 / 业务工具
# ----------------------------------------------------------------------------
def parse_dt(s):
    """兼容 '20260618071033' 与 '2026-06-17 18:00:00' -> date"""
    digits = re.sub(r"\D", "", s or "")
    return datetime.strptime(digits[:8], "%Y%m%d").date()


def add_months(d, months):
    idx = d.month - 1 + months
    y = d.year + idx // 12
    m = idx % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def months_ago(d, months):
    return add_months(d, -months)


def fmt_date(d):
    return d.strftime("%Y-%m-%d") if isinstance(d, date) else str(d)


def flow_kind(item):
    fid = item.get("flowId", "") or ""
    if any(fid.startswith(p) for p in OVERTIME_PREFIXES):
        return "加班"
    if any(fid.startswith(p) for p in TIAOXIU_PREFIXES):
        return "调休"
    return None


def dedupe(items):
    by_sn, dropped = {}, 0
    for it in items:
        sn = it.get("flowInsSN") or it.get("flowInsId")
        cur = by_sn.get(sn)
        if cur is None:
            by_sn[sn] = it
        else:
            dropped += 1
            if it.get("flowFlag") == FLAG_DONE and cur.get("flowFlag") != FLAG_DONE:
                by_sn[sn] = it
    return list(by_sn.values()), dropped


# ----------------------------------------------------------------------------
# .net 接口（tokenId7 鉴权）
# ----------------------------------------------------------------------------
def net_session(ctx):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA_WECHAT,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE_NET}/vcardoah5/oa/list?tabId=wfqd",
    })
    if ctx.get("tokenId7"):
        s.cookies.set("tokenId7", ctx["tokenId7"], domain="kk.xwtec.net")
    return s


def probe_token(ctx):
    """校验 tokenId7 是否有效，并顺带取用户名。返回 {valid, name}。"""
    if not ctx.get("tokenId7"):
        return {"valid": False, "name": None}
    try:
        r = net_session(ctx).post(f"{BASE_NET}/vcardoah5/oa/list",
                                  data=_list_body(1), timeout=12)
        if not r.headers.get("content-type", "").startswith("application/json"):
            return {"valid": False, "name": None}
        r.encoding = "utf-8"
        d = r.json()
        if "common" not in d:
            return {"valid": False, "name": None}
        name = d["common"][0].get("applyUserName") if d.get("common") else ctx.get("name")
        ctx["name"] = name
        return {"valid": True, "name": name}
    except Exception:
        return {"valid": False, "name": None}


def _list_body(size):
    return {"tabId": "wfqd", "flowFlag": "", "flowId": "", "flowName": "",
            "flowInsTitle": "", "flowInsSN": "", "startApplyTime": "",
            "endApplyTime": "", "size": size}


def fetch_oa_list(session, cutoff):
    """oa/list 按 size 返回最新若干条；逐步加大 size 直到覆盖 cutoff 或没有更多。"""
    size = 60
    common = []
    while True:
        r = session.post(f"{BASE_NET}/vcardoah5/oa/list", data=_list_body(size), timeout=20)
        r.encoding = "utf-8"
        d = r.json()
        common = d.get("common", []) or []
        if not d.get("isMore") or not common or size >= 600:
            break
        oldest = min(parse_dt(c.get("applyTime", "20990101")) for c in common)
        if oldest < cutoff:
            break
        size *= 2
    return [c for c in common if parse_dt(c.get("applyTime", "19700101")) >= cutoff]


def fetch_oa_detail(session, flow_id, flow_ins_id):
    """打开 oa/next 详情页，解析表单字段 -> {label: value}。"""
    r = session.get(f"{BASE_NET}/vcardoah5/oa/next",
                    params={"flowId": flow_id, "flowInsId": flow_ins_id}, timeout=20)
    r.encoding = "utf-8"
    html = r.text
    pairs = re.findall(
        r'd-main__title[^>]*>(.*?)</div>\s*<div class="d-main__content[^>]*>(.*?)</div>',
        html, re.S)
    fields = {}
    for t, c in pairs:
        t = re.sub(r"<[^>]+>", "", t).strip()
        c = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
        if t:
            fields[t] = c
    return fields


def _hours_from_fields(fields):
    for label, val in fields.items():
        if "时长" in label:
            m = re.search(r"[\d.]+", val or "")
            if m:
                return float(m.group())
    return 0.0


def build_report(ctx):
    """拉取近三个月加班/调休并按到期规则结算。"""
    session = net_session(ctx)
    today = date.today()
    cutoff = months_ago(today, MONTHS)

    records, dropped = dedupe(fetch_oa_list(session, cutoff))
    if records:
        ctx["name"] = records[0].get("applyUserName") or ctx.get("name")

    def rows_of(kind, date_label):
        rows = []
        for it in records:
            if flow_kind(it) != kind:
                continue
            fields = fetch_oa_detail(session, it.get("flowId"), it.get("flowInsId"))
            hours = _hours_from_fields(fields)
            try:
                ev = parse_dt(fields.get(date_label) or fields.get("开始时间")
                              or it.get("applyTime"))
            except Exception:
                ev = parse_dt(it.get("applyTime"))
            rows.append({
                "sn": it.get("flowInsSN", ""),
                "flag": it.get("flowFlag"),
                "status": FLAG_LABELS.get(it.get("flowFlag"), f"其他({it.get('flowFlag')})"),
                "applyDate": fmt_date(parse_dt(it.get("applyTime"))),
                "eventDate": fmt_date(ev),
                "_event": ev,
                "hours": round(hours, 1),
            })
        rows.sort(key=lambda x: x["eventDate"], reverse=True)
        return rows

    ot_rows = rows_of("加班", "结束时间")
    tx_rows = rows_of("调休", "开始时间")

    # ---- 结算：仅已通过；加班按事件日 + MONTHS 到期，FIFO 先消耗最早到期额度 ----
    credits = sorted([r for r in ot_rows if r["flag"] == FLAG_DONE], key=lambda r: r["_event"])
    used_total = sum(r["hours"] for r in tx_rows if r["flag"] == FLAG_DONE)
    remain = used_total
    for c in credits:
        expiry = add_months(c["_event"], MONTHS)
        c["expiry"], c["_expiry"] = fmt_date(expiry), expiry
        take = min(c["hours"], remain)
        c["left"] = round(c["hours"] - take, 1)
        remain -= take

    earned = round(sum(c["hours"] for c in credits), 1)
    valid_remaining = round(sum(c["left"] for c in credits if c["_expiry"] >= today), 1)
    void_hours = round(sum(c["left"] for c in credits if c["_expiry"] < today), 1)
    soon = today + timedelta(days=EXPIRE_SOON_DAYS)
    expiring_soon = round(sum(c["left"] for c in credits if today <= c["_expiry"] <= soon), 1)
    expiring_list = [
        {"sn": c["sn"], "eventDate": c["eventDate"], "expiry": c["expiry"],
         "left": c["left"], "days": (c["_expiry"] - today).days}
        for c in credits if today <= c["_expiry"] <= soon and c["left"] > 0]

    def clean(rows):
        return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]

    return {
        "user": ctx.get("name"),
        "window": {"start": fmt_date(cutoff), "end": fmt_date(today), "months": MONTHS},
        "overtime": clean(ot_rows),
        "tiaoxiu": clean(tx_rows),
        "dropped": dropped,
        "summary": {"earned": earned, "used": round(used_total, 1),
                    "remaining": valid_remaining, "voidHours": void_hours,
                    "expiringSoon": expiring_soon, "expiringDays": EXPIRE_SOON_DAYS},
        "expiringList": expiring_list,
    }


def fetch_perf(ctx):
    """绩效（同一 tokenId7）。"""
    if not ctx.get("tokenId7"):
        return {"needToken": True, "error": "未获取登录态"}
    s = net_session(ctx)
    s.headers["Referer"] = f"{BASE_NET}/vcardh5/pref/achiev"
    try:
        fr = s.get(f"{BASE_NET}/vcardh5/pref/perfTemplateFormList", timeout=15)
        pr = s.get(f"{BASE_NET}/vcardh5/pref/perfTemplatePlanList", timeout=15)
        fr.encoding = pr.encoding = "utf-8"
        is_json = lambda x: x.headers.get("content-type", "").startswith("application/json")
        forms = fr.json() if is_json(fr) else None
        plans = pr.json() if is_json(pr) else None
    except Exception as e:
        return {"needToken": True, "error": str(e)}
    if forms is None:
        return {"needToken": True, "error": "绩效登录态失效"}

    def grade_rows(items):
        out = []
        for r in items or []:
            v = str(r.get("value") or "")
            out.append({"month": f"{v[:4]}-{v[4:6]}" if len(v) >= 6 else v,
                        "name": r.get("name"), "grade": r.get("gradeName") or "—",
                        "final": r.get("finalGrade"), "self": r.get("selfGrade"),
                        "early": r.get("earlyGrade"), "check": r.get("checkGrade")})
        out.sort(key=lambda x: x["month"], reverse=True)
        return out

    plan_rows = []
    for p in plans or []:
        v = str(p.get("value") or "")
        plan_rows.append({"name": p.get("name"),
                          "month": f"{v[:4]}-{v[4:6]}" if len(v) >= 6 else v,
                          "start": p.get("startTime"), "end": p.get("endTime")})
    return {"needToken": False, "forms": grade_rows(forms), "plans": plan_rows}


# ----------------------------------------------------------------------------
# 从本机 PC 微信 cookie 库读取 tokenId7（绩效是微信授权，token 存在微信 webview）
# ----------------------------------------------------------------------------
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


# 扫描时跳过的大目录（聊天记录/媒体），避免慢；注意不跳 cache（老版微信 CEF 的
# Cookies 可能就在 Cache 目录里）。
_SKIP_DIRS = {"msg", "filestorage", "backup", "video", "image", "img",
              "emoji", "filecache", "crashpad", "temp", "tmp"}


def _candidate_cookie_dbs(extra_roots=None):
    """递归发现本机微信所有 Chromium cookie 库（不依赖固定版本路径）。
    extra_roots：用户手动指定的目录或 Cookies 文件路径（支持 %APPDATA% / ~）。"""
    home = os.path.expanduser("~")
    prof = os.environ.get("USERPROFILE") or home
    bases = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
             os.path.join(home, "AppData", "Roaming"),
             os.path.join(home, "AppData", "Local")]
    roots, direct = [], []
    for base in bases:
        if base:
            for brand in ("Tencent", "WeChat", "Weixin", "xwechat"):
                roots.append(os.path.join(base, brand))
    # 老版本可能把数据放在“文档”下
    roots.append(os.path.join(prof, "Documents", "WeChat Files"))
    roots.append(os.path.join(prof, "Documents", "xwechat_files"))
    for r in (extra_roots or []):
        p = os.path.normpath(os.path.expandvars(os.path.expanduser(str(r).strip().strip('"'))))
        if os.path.isfile(p):
            direct.append(p)
        elif os.path.isdir(p):
            roots.append(p)
    dbs, seen_root = list(direct), set()
    for root in roots:
        root = os.path.normpath(root)
        if not os.path.isdir(root) or root in seen_root:
            continue
        seen_root.add(root)
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


def _query_token_enc(db_path):
    """从 cookie 库取出 kk.xwtec.net 的 tokenId7 密文（容忍 host 带不带点）。"""
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "c.db")
    try:
        shutil.copy(db_path, dst)
        con = sqlite3.connect(dst)
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


def _decrypt_token_from_dbs(dbs):
    """从给定 cookie 库列表里解出 tokenId7；用所有 Local State 密钥兜底。"""
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


def read_wechat_tokenid7(extra_roots=None):
    """自动发现并解出本机微信里 kk.xwtec.net 的 tokenId7。失败返回 None。"""
    if sys.platform != "win32":
        return None
    return _decrypt_token_from_dbs(_candidate_cookie_dbs(extra_roots))


# ----------------------------------------------------------------------------
# Flask
# ----------------------------------------------------------------------------
app = Flask(__name__)
COOKIE_AGE = 30 * 24 * 3600
GITHUB_URL = "https://github.com/qiyh99"
FAVICON_B64 = 'AAABAAEAEBAAAAEAIABoBAAAFgAAACgAAAAQAAAAIAAAAAEAIAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAD////////////////+/v///eHO//ykaP/8fCb//WkG//1pBv/8fCb//KRo//3hzv/+/v7////////////////////////////++fX//KJl//1mAv/9ZgH//mYB//5mAf/+ZgH//mYB//1mAP/9ZgL//KJl//759f/////////////////++fX//Io+//1yFv/9eSH//WYB//5mAf/+ZgH//mYB//1mAP/qbgf/wnwQ//VpA//8ij7//vn1///////+/v7//KJl//2LP//+y6r//syr//2sdv/9aAT//mYB//1mAP/EgBj/GslR/wvISv8yszX/8moD//yiZf/+/v7//eHO//1mAv/9v5X//ti+//7Yv//+2L///bWE//1oBf/EhB3/GNdo/wzWYv8Lz1b/C8hK/7eBFP/9ZgL//eHO//ykaP/9ZQH//c2s//7j0v/+49L//uPS//7j0v/vxZn/HuWA/w3kef8N3W3/DNZi/xLMU//ecgr//WYA//ykaP/8fCb//WYA//3Wu//+7+T//u/l//7v5f/+7+T//u/k/7Lwyf8S64f/DeR5/xLba/+yiR///WYB//5mAf/8fCb//WkG//1mAf/92cD///Lq///y6v//8ur///Lq///y6v/+8ur/svHM/xjohP+yjSb//WYB//5mAf/+ZgH//WkG//1pBv/9ZgH//dnA///y6v//8ur///Lq///y6v//8ur///Lq//7y6v/o0az//WgF//5mAf/+ZgH//mYB//1pBv/8fCb//WYB//3ZwP//8ur///Lq//7y6v/80rX///Lq///y6v//8ur//vLq//3Fn//9aAX//mYB//5mAf/8fCb//KNo//1mAf/92cD///Lq///y6v/+8ur//HIV//3Nrf/+8ur///Lq///y6v/+8ur//cKb//1mAf/9ZgH//KRo//3hzv/9ZgL//dnA///y6v//8ur//vLq//xsDP/9awr//c2t//7y6v//8ur///Lq//7y6v/8eiP//WYC//3hzv/+/v7//KJl//3Env//8ur///Lq//3k0v/9ZgL//mYB//1rCv/9za3///Lq///y6v/+7eH//W8R//yiZf/+/v7///////759f/8mFX//cCW//3Jpf/9gzL//mYB//5mAf/+ZgH//WsK//2zgf/9x6P//Is///yKPv/++fX//////////////////vn1//yiZf/9ZgL//mYB//5mAf/+ZgH//mYB//5mAf/+ZgH//WYC//yiZf/++fX////////////////////////////+/v7//eHO//ykaP/8fCb//WkG//1pBv/8fCb//KNo//3hzv/+/v7/////////////////AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=='


def with_sid(payload, sid):
    resp = jsonify(payload)
    resp.set_cookie("sid", sid, max_age=COOKIE_AGE, httponly=True, samesite="Lax")
    return resp


def is_local_request():
    return request.remote_addr in ("127.0.0.1", "::1", "localhost")


@app.get("/")
def index():
    return Response(PAGE_HTML, mimetype="text/html")


@app.get("/favicon.ico")
def favicon():
    return Response(base64.b64decode(FAVICON_B64), mimetype="image/x-icon")


@app.post("/api/quit")
def api_quit():
    """关闭程序（无控制台时用于退出整个服务）。"""
    def _stop():
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    ctx, sid, is_new = ensure_ctx()
    info = probe_token(ctx)
    payload = {"ready": info["valid"], "name": info.get("name"),
               "local": is_local_request()}
    return with_sid(payload, sid) if is_new else jsonify(payload)


@app.get("/api/token/auto")
def api_token_auto():
    """仅本机：从 PC 微信自动读取 tokenId7。可选 ?dir= 手动指定微信目录。"""
    ctx = current_ctx()
    if ctx is None:
        return jsonify({"ok": False, "error": "会话已失效，请刷新页面"})
    if not is_local_request():
        return jsonify({"ok": False, "error": "仅本机可自动读取微信；同事请各自在自己电脑运行"})
    manual = request.args.get("dir", "").strip()
    extra = [manual] if manual else None
    try:
        dbs = _candidate_cookie_dbs(extra)
        tk = _decrypt_token_from_dbs(dbs)
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取失败：{e}"})
    if not tk:
        if not dbs:
            return jsonify({"ok": False, "needDir": True,
                            "error": "没找到微信数据目录。请在下方手动指定微信目录（见“目录在哪”）。"})
        return jsonify({"ok": False, "needDir": True,
                        "error": "找到了微信，但没有绩效登录态。请先用电脑微信打开一次绩效/OA 页再重试。"})
    ctx["tokenId7"] = tk
    info = probe_token(ctx)
    if not info["valid"]:
        return jsonify({"ok": False, "error": "读到的登录态无效（可能已过期），请在微信里重新打开一次"})
    save_sessions()
    return jsonify({"ok": True, "name": info.get("name")})


@app.post("/api/token")
def api_token_set():
    ctx = current_ctx()
    if ctx is None:
        return jsonify({"ok": False, "error": "会话已失效，请刷新页面"})
    tk = (request.json or {}).get("tokenId7", "").strip()
    if tk.startswith("tokenId7="):
        tk = tk[len("tokenId7="):]
    if not tk:
        return jsonify({"ok": False, "error": "tokenId7 不能为空"})
    ctx["tokenId7"] = tk
    info = probe_token(ctx)
    if not info["valid"]:
        return jsonify({"ok": False, "error": "这个 tokenId7 无效或已过期"})
    save_sessions()
    return jsonify({"ok": True, "name": info.get("name")})


@app.get("/api/report")
def api_report():
    ctx = current_ctx()
    if ctx is None or not ctx.get("tokenId7"):
        return jsonify({"ok": False, "error": "未获取登录态"})
    try:
        return jsonify({"ok": True, "data": build_report(ctx)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.get("/api/perf")
def api_perf():
    ctx = current_ctx()
    if ctx is None:
        return jsonify({"ok": False, "error": "会话已失效，请刷新页面"})
    try:
        return jsonify({"ok": True, "data": fetch_perf(ctx)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.post("/api/logout")
def api_logout():
    sid = request.cookies.get("sid")
    if sid:
        SESSIONS.pop(sid, None)
        save_sessions()
    resp = jsonify({"ok": True})
    resp.delete_cookie("sid")
    return resp


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>咔咔 OA 助手</title>
<link rel="icon" href="/favicon.ico">
<style>
  :root{--bg:#0f1220;--card:#1a1f35;--mut:#8a93b2;--fg:#e8ecf8;--acc:#4f8cff;--ok:#37c871;--warn:#ffb020;--bad:#ff5d5d}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
  .wrap{max-width:960px;margin:0 auto;padding:24px 16px 60px}
  h1{font-size:20px;margin:0 0 4px} .sub{color:var(--mut);font-size:13px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid #2a3150;border-radius:14px;padding:18px;margin-bottom:16px}
  .muted{color:var(--mut);font-size:13px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .stat{background:#141a30;border:1px solid #2a3150;border-radius:12px;padding:14px}
  .stat .v{font-size:26px;font-weight:700;margin-top:4px}
  .stat .u{font-size:12px;color:var(--mut);margin-left:3px}
  .stat .l{font-size:12px;color:var(--mut)}
  .accent{color:var(--acc)} .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #242b48}
  th{color:var(--mut);font-weight:600} td.r,th.r{text-align:right}
  .pill{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid}
  .pill.done{color:var(--ok);border-color:#27623f;background:#15311f}
  .pill.undo{color:var(--mut);border-color:#39406a;background:#1c2138}
  .banner{background:#3a2a12;border:1px solid #6b4d1a;color:var(--warn);border-radius:12px;padding:12px 14px;margin-bottom:16px;font-size:14px}
  h2{font-size:16px;margin:4px 0 8px}
  input{background:#0d1426;border:1px solid #2a3150;color:var(--fg);border-radius:8px;padding:8px 10px;width:100%}
  button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:14px}
  button.ghost{background:#222a47;color:var(--fg)}
  .row{display:flex;gap:8px;align-items:center}
  a.link{color:var(--acc);text-decoration:none}
  .by{font-size:12px;font-weight:400;color:var(--mut);text-decoration:none}
  .by:hover{color:var(--acc)}
  .quit{float:right;background:#222a47;color:var(--mut);font-size:12px;padding:6px 12px}
  .quit:hover{background:#3a2030;color:var(--bad)}
  .hide{display:none}
  details{margin-top:8px} summary{cursor:pointer;color:var(--mut);font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <button class="quit" onclick="quitApp()">关闭程序</button>
  <h1>咔咔 OA 助手 <a class="by" href="GITHUB_URL" target="_blank" rel="noopener">by qiyh99</a></h1>
  <div class="sub" id="who">全自动 · 读取本机微信登录态，无需扫码/账号</div>

  <!-- 登录态获取 -->
  <div class="card" id="setupCard">
    <div id="setupMsg" class="muted">正在读取本机微信登录态…</div>
    <div id="setupActions" class="hide" style="margin-top:12px">
      <div class="row" style="margin-bottom:8px">
        <button id="autoBtn" class="hide" onclick="autoToken()">🟢 从本机微信自动获取</button>
        <span id="autoMsg" class="muted"></span>
      </div>
      <div id="dirBox" class="hide" style="margin-bottom:8px">
        <div class="row">
          <input id="dirInput" placeholder="微信目录（自动读不到时填），如 %APPDATA%\Tencent"/>
          <button class="ghost" onclick="autoTokenDir()">从该目录读取</button>
        </div>
      </div>
      <div class="row">
        <input id="tokenInput" placeholder="或手动粘贴 tokenId7=..."/>
        <button class="ghost" onclick="saveToken()">保存</button>
      </div>
      <details>
        <summary>目录在哪？/ 读不到怎么办</summary>
        <div class="muted" style="margin-top:6px">
          1. 先用<b>电脑版微信</b>打开过一次绩效 / OA 页（让微信存下登录态），再点“从本机微信自动获取”。<br>
          2. 还读不到就在上面“微信目录”框里填，然后点“从该目录读取”。默认目录（直接复制）：<br>
          &nbsp;&nbsp;<code>%APPDATA%\Tencent</code><br>
          常见完整位置（<code>用户名</code>换成你自己的）：<br>
          &nbsp;&nbsp;· 新版微信：<code>C:\Users\用户名\AppData\Roaming\Tencent\xwechat\radium\web\profiles</code><br>
          &nbsp;&nbsp;· 旧版微信：<code>C:\Users\用户名\AppData\Roaming\Tencent\WeChat\xweb</code><br>
          也可以直接填到那个名为 <code>Cookies</code> 的文件路径。<br>
          3. 实在不行：运行随附的 <b>kaka_get_token.py</b> 拿到 token，粘到最下面那个框。
        </div>
      </details>
    </div>
  </div>

  <!-- 调休结算 -->
  <div id="dash" class="hide">
    <div id="expBanner" class="banner hide"></div>
    <div class="card">
      <h2>调休结算 <span class="muted" id="winLabel"></span></h2>
      <div class="cards" id="statCards"></div>
      <div class="muted" style="margin-top:10px">规则：加班按 1:1 折算调休，自加班日起满 {MONTHS} 个月未用即作废；按“最早到期先用”估算剩余。</div>
    </div>
    <div class="card"><h2>加班申请（近{MONTHS}个月）</h2><div id="otTable"></div></div>
    <div class="card"><h2>调休申请（近{MONTHS}个月）</h2><div id="txTable"></div></div>
  </div>

  <!-- 绩效 -->
  <div id="perf" class="card hide">
    <h2>个人绩效</h2>
    <div id="perfBody" class="muted">加载中…</div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const fx = (n) => (n==null?'—':(Math.round(n*10)/10));
let IS_LOCAL = false, triedAuto = false;
async function jget(u){ return (await fetch(u)).json(); }

async function boot(){
  const st = await jget('/api/status');
  IS_LOCAL = !!st.local;
  if(st.ready){ onReady(st.name); return; }
  if(IS_LOCAL && !triedAuto){
    triedAuto = true;
    $('#setupMsg').textContent = '正在从本机微信读取登录态…';
    const a = await jget('/api/token/auto');
    if(a.ok){ onReady(a.name); return; }
    $('#setupMsg').textContent = a.error || '未能自动获取';
  } else {
    $('#setupMsg').textContent = '需要绩效登录态(tokenId7)';
  }
  $('#setupActions').classList.remove('hide');
  if(IS_LOCAL){ $('#autoBtn').classList.remove('hide'); $('#dirBox').classList.remove('hide'); }
}

function onReady(name){
  $('#setupCard').classList.add('hide');
  $('#who').innerHTML = '已就绪：' + (name||'') + ' · <a href="#" class="link" onclick="logout();return false">退出</a>';
  $('#dash').classList.remove('hide');
  $('#perf').classList.remove('hide');
  loadReport(); loadPerf();
}

async function autoToken(){
  $('#autoMsg').textContent = '读取中…';
  const r = await jget('/api/token/auto');
  if(r.ok){ onReady(r.name); return; }
  $('#autoMsg').textContent = r.error || '读取失败';
  if(r.needDir) $('#dirBox').classList.remove('hide');
}
async function autoTokenDir(){
  const dir = $('#dirInput').value.trim();
  if(!dir){ $('#autoMsg').textContent='请先填微信目录'; return; }
  $('#autoMsg').textContent = '读取中…';
  const r = await jget('/api/token/auto?dir=' + encodeURIComponent(dir));
  if(r.ok){ onReady(r.name); } else { $('#autoMsg').textContent = r.error || '读取失败'; }
}
async function saveToken(){
  const v = $('#tokenInput').value.trim();
  const r = await (await fetch('/api/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tokenId7:v})})).json();
  if(r.ok){ onReady(r.name); } else { alert(r.error); }
}
async function logout(){ await fetch('/api/logout',{method:'POST'}); location.reload(); }
async function quitApp(){
  if(!confirm('关闭咔咔 OA 助手？关闭后本页将失效。')) return;
  try{ await fetch('/api/quit',{method:'POST'}); }catch(e){}
  document.body.innerHTML = '<div style="max-width:960px;margin:80px auto;text-align:center;color:#8a93b2;font-family:sans-serif">程序已关闭，可直接关闭此标签页。</div>';
}

function statCard(label, val, unit, cls){
  return `<div class="stat"><div class="l">${label}</div><div class="v ${cls||''}">${fx(val)}<span class="u">${unit||''}</span></div></div>`;
}
function table(cols, rows){
  let h = '<table><thead><tr>' + cols.map(c=>`<th class="${c.r?'r':''}">${c.t}</th>`).join('') + '</tr></thead><tbody>';
  if(!rows.length) h += `<tr><td colspan="${cols.length}" class="muted">无记录</td></tr>`;
  for(const row of rows) h += '<tr>' + cols.map(c=>`<td class="${c.r?'r':''}">${c.render?c.render(row):(row[c.k]??'')}</td>`).join('') + '</tr>';
  return h + '</tbody></table>';
}
const statusPill = r => `<span class="pill ${r.flag===4?'done':'undo'}">${r.status}</span>`;

async function loadReport(){
  const r = await jget('/api/report');
  if(!r.ok){ $('#statCards').innerHTML = '<div class="muted">加载失败：'+r.error+'</div>'; return; }
  const d = r.data, s = d.summary;
  $('#winLabel').textContent = `${d.window.start} ~ ${d.window.end}`;
  $('#statCards').innerHTML =
      statCard('已通过加班', s.earned, 'h') + statCard('已用调休', s.used, 'h')
    + statCard('剩余可调休', s.remaining, 'h', 'ok')
    + statCard(`${s.expiringDays}天内到期`, s.expiringSoon, 'h', s.expiringSoon>0?'warn':'')
    + statCard('已作废', s.voidHours, 'h', s.voidHours>0?'bad':'');
  if(d.expiringList && d.expiringList.length){
    $('#expBanner').classList.remove('hide');
    $('#expBanner').innerHTML = '⏰ ' + d.expiringList.map(e=>
      `${e.left}h 将在 ${e.expiry}（${e.days}天后）作废（加班 ${e.eventDate} / ${e.sn}）`).join('<br>');
  } else { $('#expBanner').classList.add('hide'); }
  $('#otTable').innerHTML = table([
    {t:'加班日期',k:'eventDate'}, {t:'申请日',k:'applyDate'}, {t:'单号',k:'sn'},
    {t:'状态',render:statusPill}, {t:'时长',r:true,render:r=>fx(r.hours)+'h'}, {t:'到期',k:'expiry'}
  ], d.overtime);
  $('#txTable').innerHTML = table([
    {t:'调休日期',k:'eventDate'}, {t:'申请日',k:'applyDate'}, {t:'单号',k:'sn'},
    {t:'状态',render:statusPill}, {t:'时长',r:true,render:r=>fx(r.hours)+'h'}
  ], d.tiaoxiu);
}

async function loadPerf(){
  $('#perfBody').innerHTML = '加载中…';
  const r = await jget('/api/perf');
  if(!r.ok){ $('#perfBody').textContent = '加载失败：'+r.error; return; }
  const d = r.data;
  if(d.needToken){ $('#perfBody').textContent = d.error || '绩效登录态失效'; return; }
  let html = '';
  if(d.plans && d.plans.length)
    html += '<div class="muted" style="margin-bottom:8px">进行中：' +
      d.plans.map(p=>`${p.name} ${p.month}（${p.start}~${p.end}）`).join('；') + '</div>';
  html += table([
    {t:'月份',k:'month'}, {t:'名称',k:'name'}, {t:'等级',render:r=>`<b class="accent">${r.grade}</b>`},
    {t:'最终',r:true,k:'final'}, {t:'自评',r:true,k:'self'}, {t:'初评',r:true,k:'early'}, {t:'终评',r:true,k:'check'}
  ], d.forms);
  $('#perfBody').innerHTML = html;
}

boot();
</script>
</body>
</html>""".replace("{MONTHS}", str(MONTHS)).replace("GITHUB_URL", GITHUB_URL)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def open_browser():
    webbrowser.open(f"http://127.0.0.1:{PORT}/")


def main():
    # 无控制台(windowed)运行时 stdout/stderr 可能为 None，兜底避免 print / 日志崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    load_sessions()
    ip = local_ip()
    print("=" * 54)
    print("  咔咔 OA 助手（全自动）已启动")
    print(f"  本机访问：http://127.0.0.1:{PORT}/   （自动读本机微信，零操作）")
    print(f"  局域网：  http://{ip}:{PORT}/   （同事访问需各自提供登录态）")
    print("=" * 54)
    threading.Timer(1.0, open_browser).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
