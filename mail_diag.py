# -*- coding: utf-8 -*-
"""诊断 Gmail 收件箱：列出最近的邮件，找出 GLaDOS 验证码邮件的真实特征。

本地跑需要走代理（imaplib 不原生支持 socks，用 monkeypatch）。
"""
import email
import imaplib
import os
import re
import socket
import sys

USER = "izayaki5452@gmail.com"
PW = os.environ.get("GMAIL_PW", "").replace(" ", "")

# 走本地代理（国内直连 imap.gmail.com 不通）
try:
    import socks
    socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", 7897)
    socket.socket = socks.socksocket
    print("已启用 SOCKS5 代理 127.0.0.1:7897")
except ImportError:
    print("⚠️  没装 pysocks，尝试直连")


def body_of(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    pass
        return ""
    try:
        return msg.get_payload(decode=True).decode("utf-8", "replace")
    except Exception:
        return ""


def main():
    if not PW:
        raise SystemExit("需要 GMAIL_PW 环境变量")

    M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    M.login(USER, PW)
    print("✅ IMAP 登录成功\n")
    M.select("INBOX")

    typ, data = M.search(None, "ALL")
    ids = data[0].split()[-15:]
    print(f"最近 {len(ids)} 封邮件:\n")

    for mid in reversed(ids):
        typ, md = M.fetch(mid, "(RFC822)")
        msg = email.message_from_bytes(md[0][1])
        subj = str(email.header.make_header(email.header.decode_header(msg.get("Subject") or "")))
        frm = str(msg.get("From") or "")
        date = str(msg.get("Date") or "")
        body = body_of(msg)

        print(f"── {date}")
        print(f"   From   : {frm[:90]}")
        print(f"   Subject: {subj[:90]}")

        # 找所有 4~8 位数字，看看能提到什么
        nums = re.findall(r"\b\d{4,8}\b", body)[:6]
        print(f"   正文里的数字: {nums}")
        print(f"   正文长度: {len(body)}")
        print()

    M.logout()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
