"""设备校准覆盖审计测试（半开区间合规/空窗分段）。

覆盖：
- 计入起点 max(签发创建时刻, 有效期起点)、退出 min(有效期终点, 撤销/级联失效时刻)；
- 半开边界：cover_from 时刻合规、cover_to/revoked_at 时刻空窗；
- 片段完整、无重叠、按时间顺序铺满窗口，相邻同状态同编号合并、不同编号不合并；
- 上游证书有效期只按下游校准时刻的既有签发规则判断：上游事后自然到期不取消
  下游覆盖；上游事后撤销，下游按级联失效时刻截断；
- 历史重签即使有效期重叠也按实际签发/失效事件还原（旧证覆盖不被新证替代）；
- FAIL 证书不计入；基准证书不是“普通证书”，不计入；
- 未知设备整段空窗；参数错误（缺参/无法解析/无时区/start>=end/end 在未来）422
  invalid_audit_window；
- 既有签发/撤销/批次/归档接口契约不受影响（基础冒烟）。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/coverage_checks.py [base_url]
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
results = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def coverage(device, start, end):
    qs = urllib.parse.urlencode({"start": start, "end": end})
    return call("GET", f"/api/v1/devices/{urllib.parse.quote(device)}/coverage?{qs}")


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


def err_code(body):
    return body.get("error", {}).get("code")


TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ).replace(microsecond=0)


def ts(offset_days=0.0):
    return (NOW + timedelta(days=offset_days)).isoformat()


def settle():
    """签发后等待，确保 created_at 稳定早于“当前时刻前 1 秒”的审计终点。"""
    time.sleep(1.5)


def settle_ts():
    return (datetime.now(TZ) - timedelta(seconds=1)).replace(microsecond=0).isoformat()


def ref(no, f, to, calibrated=None, device=None):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": device or "DEV-" + no,
        "valid_from": ts(f), "valid_to": ts(to),
        "calibrated_at": ts(calibrated if calibrated is not None else f)})


def cal(no, device, parent, f, to, calibrated=None, conclusion="PASS"):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": device,
        "valid_from": ts(f), "valid_to": ts(to),
        "calibrated_at": ts(calibrated if calibrated is not None else f),
        "conclusion": conclusion, "parent_cert_no": parent})


def seg_shape(body):
    """仅保留状态/编号；边界做秒级容差后转相对小时数，便于核对顺序与铺满。"""
    out = []
    for s in body["segments"]:
        out.append((s["status"], tuple(s["cert_nos"]), s["start"], s["end"]))
    return out


# ---------------------------------------------------------------------------
print("== 0. 前置数据：共享宽有效期基准 + 一条将自然到期的基准 + 一条将被撤销的基准 ==")
for s, _ in [
    ref("REF-COV", -300, 300),        # 共享基准，全程 ACTIVE
    ref("REF-COV-E", -300, -2),       # 2 天前自然到期，但不会被自动撤销
    ref("REF-COV-R", -300, 300),      # 稍后将被主动撤销
]:
    check("前置基准 201", s == 201, f"got {s}")

# ---------------------------------------------------------------------------
print("== A. 计入起点 = max(创建时刻, 有效期起点)：有效期回填，覆盖从创建时刻才开始 ==")
s, ca = cal("CAL-COV-A", "COV-A", "REF-COV", -100, 100)
check("签发回填有效期的普通证书 201", s == 201, f"got {s}")
created_a = ca["certificate"]["created_at"]
settle()
end_now = settle_ts()

s, body = coverage("COV-A", ts(-200), end_now)
check("覆盖审计 200", s == 200, f"got {s}")
check("回显设备与窗口、含 queried_at",
      body["device_id"] == "COV-A" and body["start"].startswith(ts(-200)[:22])
      and body["end"].startswith(end_now[:19]) and bool(body["queried_at"]),
      json.dumps({k: body.get(k) for k in ("device_id", "start", "end", "queried_at")},
                 ensure_ascii=False))
statuses = [(x[0], x[1]) for x in seg_shape(body)]
check("回填证书：先空窗后合规，合规段仅列该合格普通证书",
      statuses == [("GAP", ()), ("COVERED", ("CAL-COV-A",))], str(statuses))
check("合规段起点恰为证书创建时刻（而非更早的 valid_from）",
      body["segments"][1]["start"] == created_a,
      f"{body['segments'][1]['start']} vs {created_a}")
check("片段从审计起点开始、到审计终点结束且首尾相接",
      body["segments"][0]["start"] == body["start"]
      and body["segments"][-1]["end"] == body["end"]
      and all(a["end"] == b["start"]
              for a, b in zip(body["segments"], body["segments"][1:])),
      json.dumps(seg_shape(body), ensure_ascii=False))

# 半开边界：以创建时刻为起点的窗口，第一刻即合规（不含创建时刻之前）
s, at0 = coverage("COV-A", created_a, end_now)
check("半开：cover_from 时刻合规",
      s == 200 and at0["segments"][0]["status"] == "COVERED"
      and at0["segments"][0]["cert_nos"] == ["CAL-COV-A"], str(seg_shape(at0)))

# ---------------------------------------------------------------------------
print("== B. 上游事后自然到期不取消下游覆盖 ==")
# 校准时刻 (-180d) 落在基准有效期 [-300d,-2d) 内；基准此刻虽已自然到期，
# 但状态仍 ACTIVE——下游覆盖不因其事后到期而中断。
s, ce = cal("CAL-COV-E", "COV-E", "REF-COV-E", -180, 180)
check("引用签发后自然到期基准（校准时刻在其有效期内）201", s == 201, f"got {s}")
created_e = ce["certificate"]["created_at"]
settle()
end_now = settle_ts()
# 窗口从本证实际创建时刻起：回填有效期不追溯，但创建之后到审计终点整段合规——
# 尽管其上游基准在 2 天前已自然到期，覆盖没有因此中断。
s, be = coverage("COV-E", created_e, end_now)
statuses = [(x[0], x[1]) for x in seg_shape(be)]
check("上游到期后下游仍整段合规（单个 COVERED 片段）",
      s == 200 and statuses == [("COVERED", ("CAL-COV-E",))], str(statuses))
check("下游覆盖不因上游事后自然到期而中断（无空窗片段）",
      all(x[0] == "COVERED" for x in statuses) and len(statuses) == 1, str(statuses))

# ---------------------------------------------------------------------------
print("== C. 上游事后撤销：下游按级联失效时刻截断（含半开边界） ==")
s, crm = cal("CAL-COV-RM", "COV-RM", "REF-COV-R", -100, 100)
check("撤销链上的下游证书 201", s == 201, f"got {s}")
created_rm = crm["certificate"]["created_at"]
s, rev = call("POST", "/api/v1/certificates/REF-COV-R/revoke",
              {"reason": "基准超差", "operator": "qa"})
check("撤销基准 200", s == 200, f"got {s}")
revoked_at = rev["revoked_at"]
check("下游级联失效",
      sorted(a["cert_no"] for a in rev["affected"]) == ["CAL-COV-RM"],
      str(rev.get("affected")))
settle()
end_now = settle_ts()

# [级联失效时刻, 之后) 整段空窗——失效时刻本身已不再合规
s, after = coverage("COV-RM", revoked_at, end_now)
statuses = [(x[0], x[1]) for x in seg_shape(after)]
check("revoked_at 起为空窗（半开，失效时刻退出）",
      s == 200 and statuses == [("GAP", ())], str(statuses))

# 从本证创建时刻起：先 COVERED（创建→级联失效），恰在 revoked_at 切为 GAP
s, around = coverage("COV-RM", created_rm, end_now)
segs = around["segments"]
check("失效截断：先 COVERED 后 GAP，边界恰为级联失效时刻",
      [(x["status"], tuple(x["cert_nos"])) for x in segs]
      == [("COVERED", ("CAL-COV-RM",)), ("GAP", ())]
      and segs[0]["start"] == created_rm
      and segs[0]["end"] == revoked_at and segs[1]["start"] == revoked_at,
      json.dumps([(x["status"], x["cert_nos"], x["start"], x["end"]) for x in segs],
                 ensure_ascii=False))

# ---------------------------------------------------------------------------
print("== D. 历史重签：旧证撤销后新证有效期与旧证重叠，按实际事件还原（不被当前态替代） ==")
s, old = cal("OLD-RESIGN-1", "COV-RESIGN", "REF-COV", -100, 100)
check("旧证签发 201", s == 201, f"got {s}")
old_created = old["certificate"]["created_at"]
s, r2 = call("POST", "/api/v1/certificates/OLD-RESIGN-1/revoke", {"reason": "证书录入差错重签"})
check("旧证撤销 200", s == 200, f"got {s}")
old_revoked = r2["revoked_at"]
# 新证有效期回填到 -50d，与旧证有效期 [-100d,+100d] 重叠；旧证已 REVOKED，区间释放
s, new = cal("NEW-RESIGN-1", "COV-RESIGN", "REF-COV", -50, 50)
check("重叠有效期重签成功（历史区间已释放）201", s == 201, f"got {s}")
new_created = new["certificate"]["created_at"]
settle()
end_now = settle_ts()

s, br = coverage("COV-RESIGN", ts(-200), end_now)
seq = [(x["status"], tuple(x["cert_nos"])) for x in seg_shape(br)]
check("片段序列：空窗-旧证-空窗-新证（历史状态按事件还原）",
      seq == [("GAP", ()), ("COVERED", ("OLD-RESIGN-1",)),
              ("GAP", ()), ("COVERED", ("NEW-RESIGN-1",))], str(seq))
bounds = [(x[2], x[3]) for x in seg_shape(br)]
check("各边界=旧创建/撤销/新创建时刻（新证不追溯覆盖撤销前的过去）",
      bounds[1] == (old_created, old_revoked)
      and bounds[2] == (old_revoked, new_created)
      and bounds[3][0] == new_created,
      json.dumps(bounds, ensure_ascii=False))
check("旧证覆盖段编号不被当前新证替代",
      br["segments"][1]["cert_nos"] == ["OLD-RESIGN-1"]
      and "NEW-RESIGN-1" not in br["segments"][1]["cert_nos"])

# ---------------------------------------------------------------------------
print("== E. FAIL 证书不是合格证书：整段空窗 ==")
s, _ = cal("CAL-COV-F", "COV-F", "REF-COV", -100, 100, conclusion="FAIL")
check("FAIL 普通证书可签发", s == 201, f"got {s}")
settle()
s, bf = coverage("COV-F", ts(-200), settle_ts())
check("FAIL 设备：整段单个空窗",
      s == 200 and [(x[0], x[1]) for x in seg_shape(bf)] == [("GAP", ())],
      str(seg_shape(bf)))

# ---------------------------------------------------------------------------
print("== F. 基准证书不计入“合格普通证书”：基准设备整段空窗 ==")
s, br_ref = coverage("DEV-REF-COV", ts(-200), settle_ts())
statuses = [(x[0], x[1]) for x in seg_shape(br_ref)]
check("基准证书所在设备无普通证书覆盖 -> 整段空窗",
      s == 200 and statuses == [("GAP", ())], str(statuses))

# ---------------------------------------------------------------------------
print("== G. 未知设备：200 + 整段单个空窗（不报错） ==")
s, bu = coverage("DEV-NEVER-SEEN-xyz", ts(-10), ts(-5))
check("未知设备 200", s == 200, f"got {s}")
check("整段单个空窗且铺满请求区间",
      len(bu["segments"]) == 1
      and bu["segments"][0]["status"] == "GAP"
      and bu["segments"][0]["cert_nos"] == []
      and bu["segments"][0]["start"] == bu["start"]
      and bu["segments"][0]["end"] == bu["end"],
      json.dumps(bu, ensure_ascii=False))

# ---------------------------------------------------------------------------
print("== H. 参数错误统一为 422 invalid_audit_window ==")
def raw_coverage(qs):
    req = urllib.request.Request(
        BASE + "/api/v1/devices/COV-A/coverage" + qs, method="GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

s, e = raw_coverage("")
check("缺少 start/end -> 422", s == 422 and err_code(e) == "invalid_audit_window", f"{s} {err_code(e)}")
s, e = raw_coverage("?" + urllib.parse.urlencode(
    {"start": "not-a-timestamp", "end": ts(0)}))
check("无法解析的时刻 -> 422", s == 422 and err_code(e) == "invalid_audit_window", f"{s} {err_code(e)}")
s, e = raw_coverage("?" + urllib.parse.urlencode(
    {"start": "2026-03-01T00:00:00", "end": ts(0)}))
check("不带时区的时刻 -> 422", s == 422 and err_code(e) == "invalid_audit_window", f"{s} {err_code(e)}")
s, e = coverage("COV-A", ts(0), ts(0))
check("start == end -> 422", s == 422 and err_code(e) == "invalid_audit_window", f"{s} {err_code(e)}")
s, e = coverage("COV-A", ts(0), ts(-1))
check("start > end -> 422", s == 422 and err_code(e) == "invalid_audit_window", f"{s} {err_code(e)}")
s, e = coverage("COV-A", ts(-1), ts(1))
check("end 晚于查询时刻 -> 422", s == 422 and err_code(e) == "invalid_audit_window",
      f"{s} {err_code(e)}")

# end == 服务器报告的查询时刻（半开，不含未来）合法：先取服务器时刻，避免时钟偏差
s, probe = coverage("COV-A", ts(-10), ts(-5))
server_now = probe["queried_at"]
s, bnow = coverage("COV-A", ts(-0.001), server_now)
check("end == 查询时刻（半开，不含未来）合法",
      s == 200 and bnow["segments"][-1]["end"] == bnow["end"], f"got {s}")

# ---------------------------------------------------------------------------
print("== I. 片段通用性质：按时间顺序、完整、无重叠 ==")
s, ball = coverage("COV-RESIGN", ts(-200), settle_ts())
segs = ball["segments"]
ordered = all(segs[i]["end"] == segs[i + 1]["start"] for i in range(len(segs) - 1))
check("相邻片段首尾相接（无重叠无空隙）", ordered)
check("首片段起点=窗口起点、末片段终点=窗口终点",
      segs[0]["start"] == ball["start"] and segs[-1]["end"] == ball["end"])
check("GAP 片段 cert_nos 必为空、COVERED 片段必非空",
      all((x["status"] == "GAP") == (len(x["cert_nos"]) == 0) for x in segs))
check("编号在片段内升序", all(list(x["cert_nos"]) == sorted(x["cert_nos"]) for x in segs))

# ---------------------------------------------------------------------------
print("== J. 既有接口契约保持兼容 ==")
s, c = call("GET", "/api/v1/certificates/CAL-COV-A")
check("证书详情契约不变", s == 200 and c["cert_no"] == "CAL-COV-A" and "status" in c, f"got {s}")
s, c = call("GET", "/api/v1/devices/COV-A/certificates")
check("设备历史契约不变", s == 200 and isinstance(c, list) and len(c) >= 1, f"got {s}")
s, c = call("GET", "/health")
check("健康检查不变", s == 200 and c.get("status") == "ok", f"got {s}")

# ---------------------------------------------------------------------------
fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
