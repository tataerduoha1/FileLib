"""tests/test_perm_merge.py —— 多库权限合并语义回归

针对真实故障：给普通用户配权限时，勾了多个库，
点其中一个库的「全部」或明细项，其他库的勾选会被冲掉。
（旧 set_nav_map 先 DELETE 全表、再用传入的库覆盖 users.perms）

改动 set_nav_map / update(perms=...) / 后台权限界面时必须重跑：
    python tests\test_perm_merge.py          # 默认 8321
    python tests\test_perm_merge.py 8899     # 指定端口，账号用环境变量 FL_TEST_USER/PASS
"""
import json, os, sys
import urllib.request, urllib.error, http.cookiejar

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8321
BASE = f"http://127.0.0.1:{PORT}"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"X-Requested-With": "FileLib", "Origin": BASE}
    if body is not None: h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with op.open(r, timeout=15) as x: return x.status, json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}

P = F = 0
def ck(name, cond, extra=""):
    global P, F
    if cond: P += 1; print(f"  \033[32m✓\033[0m {name}")
    else:    F += 1; print(f"  \033[31m✗\033[0m {name}  {extra}")

c, d = req("POST", "/api/login", {"username": os.environ.get("FL_TEST_USER", ""), "password": os.environ.get("FL_TEST_PASS", "")})
if c != 200:
    print("登录失败", c); sys.exit(2)

# 取前两个库
c, d = req("GET", "/api/admin/overview")
vols = sorted((d.get("volumes") or []), key=lambda v: v["id"])[:2]
if len(vols) < 2:
    print("库不足 2 个，无法测多库场景"); sys.exit(2)
V1, V2 = vols[0]["id"], vols[1]["id"]
print(f"用库：#{V1} 与 #{V2}\n")

# 建临时用户
req("POST", "/api/admin/users", {"action": "del", "id": next((u["id"] for u in req("GET","/api/admin/users")[1].get("users",[]) if u["username"]=="zz_perm_merge"), 0), "x":1})
c, d = req("POST", "/api/admin/users", {"action": "add", "username": "zz_perm_merge", "password": "Probe12345", "role": "user"})
UID = d["user"]["id"]
ck("建临时用户", c == 200)

# 步骤1：勾选两个库（perms）
c, _ = req("POST", "/api/admin/users", {"action": "perms", "id": UID, "perms": [V1, V2]})
ck("勾选两个库", c == 200)

# 步骤2：给 V1 设全部（用户报障的操作）
c, _ = req("POST", "/api/admin/users", {"action": "nav", "id": UID, "nav": {str(V1): "*"}})
c, d = req("GET", "/api/admin/users")
u = next(x for x in d["users"] if x["id"] == UID)
ck("V1 设全部后，两个库的勾选【都还在】", sorted(u["perms"]) == sorted([V1, V2]), str(u["perms"]))
ck("V1 的 nav 是 *", u["nav"].get(str(V1)) == "*", str(u.get("nav")))

# 步骤3：给 V2 配部分明细（只发 V2）
c, _ = req("POST", "/api/admin/users", {"action": "nav", "id": UID, "nav": {str(V2): ["home", "tree", "play"]}})
c, d = req("GET", "/api/admin/users")
u = next(x for x in d["users"] if x["id"] == UID)
ck("V2 配明细后，V1 的 * 【没被冲掉】", u["nav"].get(str(V1)) == "*", str(u.get("nav")))
ck("V2 明细正确", sorted(u["nav"].get(str(V2), [])) == ["home", "play", "tree"], str(u["nav"].get(str(V2))))
ck("两个库勾选仍都在", sorted(u["perms"]) == sorted([V1, V2]), str(u["perms"]))

# 步骤4：取消 V2 的全部明细（发空列表）→ 库勾选应保留，明细清空
c, _ = req("POST", "/api/admin/users", {"action": "nav", "id": UID, "nav": {str(V2): []}})
c, d = req("GET", "/api/admin/users")
u = next(x for x in d["users"] if x["id"] == UID)
ck("V2 清明细后，库勾选保留", V2 in u["perms"], str(u["perms"]))
ck("V2 明细已清", not u["nav"].get(str(V2)), str(u["nav"].get(str(V2))))
ck("V1 的 * 仍在", u["nav"].get(str(V1)) == "*", str(u.get("nav")))

# 步骤5：取消 V1 库勾选（perms 只留 V2）→ V1 的授权行应被清理，普通用户实际不可访问 V1
c, _ = req("POST", "/api/admin/users", {"action": "perms", "id": UID, "perms": [V2]})
c, d = req("GET", "/api/admin/users")
u = next(x for x in d["users"] if x["id"] == UID)
ck("perms 只剩 V2", u["perms"] == [V2], str(u["perms"]))
ck("V1 的授权行已清理", not u["nav"].get(str(V1)), str(u.get("nav")))

# 步骤6：普通用户实测——V2 只有 home/tree/play 权限
c, _ = req("POST", "/api/admin/users", {"action": "nav", "id": UID, "nav": {str(V2): ["home", "tree", "play"]}})
cj2 = http.cookiejar.CookieJar()
op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj2))
def req2(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"X-Requested-With": "FileLib", "Origin": BASE}
    if body is not None: h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with op2.open(r, timeout=15) as x: return x.status, json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}
c, d = req2("POST", "/api/login", {"username": "zz_perm_merge", "password": "Probe12345"})
ck("临时用户能登录", c == 200)
c, d = req2("GET", "/api/me")
ck("只看到 V2", d.get("vols") == [V2], str(d.get("vols")))
c, d = req2("GET", f"/api/list?vol={V2}&limit=1")
ck("V2 列表可读（home 授权）", c == 200, str(c))
c, d = req2("GET", f"/api/list?vol={V1}&limit=1")
ck("V1 被拒（未授权）", c in (403, 400), str(c))

# 清理
req("POST", "/api/admin/users", {"action": "del", "id": UID})
print(f"\n{'='*50}\n  通过 {P} · 失败 {F} · 共 {P+F}\n{'='*50}")
sys.exit(1 if F else 0)
