# -*- coding: utf-8 -*-
"""GLaDOS 自动重新登录 —— 走「邮箱验证码」流程，无需人工参与。

流程:
    1. POST /api/authorization  → 让服务端把验证码发到邮箱（SendGrid 发送）
    2. IMAP 连 Gmail 读最新那封验证码邮件
    3. POST /api/login          → 用验证码换新的 koa:sess
    4. 把新 cookie 写到文件 + 更新 GitHub secret（供下次运行直接用）

需要三个环境变量:
    GMAIL_USER          Gmail 完整地址，如 xxx@gmail.com
    GMAIL_APP_PASSWORD  Gmail 应用专用密码（16 位，不是账号密码）
    GH_PAT              有 secrets 写权限的 GitHub PAT（用于回写 cookie）

注意: 本脚本只在 GitHub Actions（海外 IP）里跑才稳，
      国内网络直连 imap.gmail.com 需要代理。
"""
import datetime
import email
import email.utils
import imaplib
import json
import os
import re
import sys
import time
import urllib.request

BASE = "https://glados.cloud"
SITE = "glados.network"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

REPO = "IRIS-5452/Glados-Railgun-checkin"
SECRET_NAME = "GLADOS_COOKIES"
NEW_COOKIE_FILE = ".new_cookie"


def log(m):
    print(m, flush=True)


def post_json(path, payload, session_cookie=None):
    req = urllib.request.Request(
        BASE + path,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Referer": BASE + "/login",
            "Origin": BASE,
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    if session_cookie:
        req.add_header("Cookie", session_cookie)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8")), r.headers


def get_body(msg):
    """取邮件纯文本正文"""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode("utf-8", "replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    pass
        return ""
    try:
        return msg.get_payload(decode=True).decode("utf-8", "replace")
    except Exception:
        return ""


FROM_RE = re.compile(r"noreply@glados\.network", re.I)
SUBJ_RE = re.compile(r"authentication code|verification code|access code|验证码", re.I)
CODE_RE = re.compile(r"\b(\d{6})\b")
MAX_AGE = 600          # 验证码邮件超过这个秒数就认为过期，不用它凑数


def fetch_mailcode(user, app_pw, timeout=240, interval=6):
    """轮询 Gmail 收件箱，抓最新的 GLaDOS 验证码。

    真实邮件特征（2026-09-26 实测）:
        From    : GLaDOS <noreply@glados.network>
        Subject : GLaDOS Authentication Code
        Body    : 内含 6 位数字

    ⚠️ 必须严格按「发件人 + 主题」过滤 —— 否则会误抓 Google 的「安全提醒」邮件，
    那封正文里有 2026 / 1600 / 94043 这类数字，会被当成验证码提交（踩过一次）。
    """
    log("  连接 imap.gmail.com …")
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    M.login(user, app_pw)
    log("  ✅ IMAP 登录成功")
    M.select("INBOX")

    deadline = time.time() + timeout
    while time.time() < deadline:
        typ, data = M.search(None, "ALL")
        if typ == "OK" and data and data[0]:
            ids = data[0].split()[-15:]          # 只看最近 15 封
            for mid in reversed(ids):            # 从最新往前找
                try:
                    typ, md = M.fetch(mid, "(RFC822)")
                    msg = email.message_from_bytes(md[0][1])
                    raw_subj = msg.get("Subject") or ""
                    subj = str(email.header.make_header(email.header.decode_header(raw_subj)))
                    frm = str(msg.get("From") or "")

                    if not FROM_RE.search(frm) or not SUBJ_RE.search(subj):
                        continue

                    # 时间检查：邮件太旧说明这轮的新码还没到，不能拿旧码凑数
                    age = None
                    try:
                        dt = email.utils.parsedate_to_datetime(msg.get("Date"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=datetime.timezone.utc)
                        age = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
                    except Exception:
                        pass
                    if age is not None and age > MAX_AGE:
                        log(f"  最新验证码邮件已 {age / 60:.0f} 分钟前，继续等新邮件…")
                        break

                    body = get_body(msg)
                    m = CODE_RE.search(body)
                    if m:
                        log(f"  找到验证码邮件（{age:.0f} 秒前）")
                        M.logout()
                        return m.group(1)
                except Exception as e:
                    log(f"  解析某封邮件失败: {str(e)[:80]}")
        time.sleep(interval)

    try:
        M.logout()
    except Exception:
        pass
    return None


def update_secret(cookie, pat):
    """把新 cookie 回写到 GitHub secret"""
    def api(path, method="GET", body=None):
        req = urllib.request.Request(
            "https://api.github.com" + path,
            method=method,
            headers={
                "Authorization": "token " + pat,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "glados-relogin",
            },
            data=json.dumps(body).encode("utf-8") if body is not None else None,
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

    from nacl import encoding, public as nacl_public
    import base64 as b64

    pk = api(f"/repos/{REPO}/actions/secrets/public-key")
    sealed = nacl_public.SealedBox(
        nacl_public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
    ).encrypt(cookie.encode())
    api(f"/repos/{REPO}/actions/secrets/{SECRET_NAME}",
        method="PUT",
        body={"encrypted_value": b64.b64encode(sealed).decode(), "key_id": pk["key_id"]})
    log(f"  ✅ 已回写 GitHub secret: {SECRET_NAME}")


def main():
    user = os.environ.get("GMAIL_USER", "").strip()
    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    pat = os.environ.get("GH_PAT", "").strip()

    if not user or not app_pw:
        log("❌ 缺少 GMAIL_USER / GMAIL_APP_PASSWORD")
        return 1

    log("=" * 50)
    log("  GLaDOS 自动重新登录")
    log("=" * 50)

    # 1. 请求发验证码
    log(f"① 请求发送验证码到 {user} …")
    try:
        r, _ = post_json("/api/authorization", {"address": user, "site": SITE})
        log(f"   返回: {r}")
        if r.get("code") != 0:
            log(f"❌ 请求验证码失败: {r.get('message')}")
            return 1
    except Exception as e:
        log(f"❌ 请求验证码异常: {e}")
        return 1

    # 2. 读邮箱
    log("② 等待验证码邮件 …")
    try:
        code = fetch_mailcode(user, app_pw)
    except imaplib.IMAP4.error as e:
        log(f"❌ IMAP 认证失败: {e}")
        log("   检查：是否用的是「应用专用密码」（16 位），不是账号密码")
        return 1
    if not code:
        log("❌ 没抓到验证码（超时）")
        return 1
    log(f"   ✅ 拿到验证码: {code}")

    # 3. 登录（用 requests 一次拿到 cookie —— 验证码是一次性的，不能提交两次）
    log("③ 提交验证码登录 …")
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": BASE + "/login", "Origin": BASE})
    try:
        resp = s.post(BASE + "/api/login", json={
            "method": "email", "site": SITE, "email": user, "mailcode": code,
        }, timeout=40)
        r = resp.json()
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        return 1

    log(f"   返回: { {k: v for k, v in r.items() if k != 'data'} }")
    if r.get("code") != 0:
        log(f"❌ 登录失败: {r.get('message')}")
        return 1

    # 注意：服务端 2026-09 把 cookie 名从 koa:sess 改成了 gld:sess。
    # 不写死名字，把 session 里的 cookie 全拼上，兼容后续改名。
    names = [c.name for c in s.cookies]
    cookie = "; ".join(f"{c.name}={c.value}" for c in s.cookies)
    if not cookie:
        log("❌ 没拿到任何 cookie")
        return 1
    log(f"   ✅ 登录成功，cookie 项: {names}（{len(cookie)} 字符）")

    # 5. 落盘 + 回写 secret
    with open(NEW_COOKIE_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(cookie + "\n")
    log(f"   ✅ 已写入 {NEW_COOKIE_FILE}")

    if pat:
        try:
            update_secret(cookie, pat)
        except Exception as e:
            log(f"   ⚠️  回写 secret 失败: {e}")
    else:
        log("   ℹ️  未提供 GH_PAT，跳过回写 secret")

    log("\n✅ 重新登录完成")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
