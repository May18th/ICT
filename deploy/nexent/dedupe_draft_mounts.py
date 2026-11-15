#!/usr/bin/env python3
"""清掉草稿层(version_no=0)的重复工具挂载。

为什么需要这个脚本
------------------
`setup_nexent.py` 的 `attach_tools_for()` 对每个工具调一次 `/api/tool/update`
并带 `version_no=0`，而那个端点每次都是 **insert 一条新 instance**，不是按
(agent, tool) upsert。所以：

    跑一次装配 → 草稿层每个工具 1 条（干净）
    再跑一次   → 每个工具 2 条（重复）

发布层(version_no>=1)是干净的——发布时平台按 tool_id 去重——但**只要有人在
平台上点一下"发布"，这份重复就会快照进新版本**，模型侧看到的是同一个工具挂两遍。

实测：2026-11-14 一天内跑了 3 次装配，草稿层重新积累出 8 条重复，所以这条不是
理论风险。

用法（在 nexent-config 容器里跑，和 setup_nexent.py 同一套鉴权）：
    docker cp deploy/nexent/dedupe_draft_mounts.py nexent-config:/tmp/
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/dedupe_draft_mounts.py"

幂等：没有重复时直接退出，返回 0。
"""

from __future__ import annotations

import os
import sys

import psycopg2

PG = dict(
    host=os.getenv("POSTGRES_HOST", "nexent-postgresql"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    user=os.getenv("POSTGRES_USER", "root"),
    password=os.getenv("NEXENT_POSTGRES_PASSWORD") or os.getenv("POSTGRES_PASSWORD"),
    dbname=os.getenv("POSTGRES_DB", "nexent"),
)

DUP_SQL = """
select ti.agent_id, ti.tool_id, t.name, count(*) n,
       string_agg(ti.tool_instance_id::text, ',' order by ti.tool_instance_id)
from nexent.ag_tool_instance_t ti
join nexent.ag_tool_info_t t on t.tool_id = ti.tool_id
where ti.version_no = 0 and ti.enabled
group by 1,2,3 having count(*) > 1
order by 1,2
"""

VICTIM_SQL = """
select ti.tool_instance_id, ti.agent_id, ti.tool_id
from nexent.ag_tool_instance_t ti
where ti.version_no = 0 and ti.enabled
  and ti.tool_instance_id > (
    select min(x.tool_instance_id) from nexent.ag_tool_instance_t x
    where x.version_no = 0 and x.enabled
      and x.agent_id = ti.agent_id and x.tool_id = ti.tool_id)
order by ti.agent_id, ti.tool_id, ti.tool_instance_id
"""

#: 草稿层启用的工具集合 vs 当前发布版本的集合。清完必须一致——
#: 少了任何一个都说明这次清理误删了（2026-11-14 真踩过）。
#: 注意：草稿集合**不能** join 发布版本表——那会把条件写成 m.v = ti.version_no
#: 又同时要求 ti.version_no = 0，恒空集，校验就永远"不一致"。
DRAFT_SET_SQL = """
select distinct t.name
from nexent.ag_tool_instance_t ti
join nexent.ag_tool_info_t t on t.tool_id = ti.tool_id
where ti.version_no = 0 and ti.enabled
  and ti.agent_id in (select distinct agent_id from nexent.ag_tenant_agent_t where delete_flag = 'N')
"""

PUBLISHED_SET_SQL = """
select distinct t.name
from nexent.ag_tool_instance_t ti
join nexent.ag_tool_info_t t on t.tool_id = ti.tool_id
join (select agent_id, max(version_no) v from nexent.ag_tenant_agent_t
      where version_no > 0 and delete_flag = 'N' group by agent_id) m
  on m.agent_id = ti.agent_id and ti.version_no = m.v
where ti.enabled
"""


def main() -> int:
    conn = psycopg2.connect(**PG)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(DUP_SQL)
    dup = cur.fetchall()
    if not dup:
        print("[dedupe] 草稿层无重复挂载，无需处理")
        return 0

    print(f"[dedupe] 发现 {len(dup)} 组重复：")
    for agent_id, tool_id, name, n, ids in dup:
        print(f"  agent={agent_id} tool={name}#{tool_id} ×{n}（instance {ids}）")

    cur.execute(VICTIM_SQL)
    victims = cur.fetchall()
    ids = [row[0] for row in victims]
    print(f"[dedupe] 保留每组最小的 tool_instance_id，删除 {len(ids)} 条：{ids}")

    cur.execute(
        "delete from nexent.ag_tool_instance_t where version_no = 0 and tool_instance_id = any(%s)",
        (ids,),
    )
    print(f"[dedupe] deleted={cur.rowcount}")

    # 保护：删完以后，草稿层启用的工具集合必须和最新发布版本一致。
    # 少一个就回滚——2026-11-14 就是因为 min 子查询没带 enabled，把 collab_run 的
    # 启用记录当成"多余副本"删了，草稿层里整个工具消失，而 8/8 仍然过（collab_run 内部
    # 也能给六跳），所以不看集合根本发现不了。
    cur.execute(DRAFT_SET_SQL)
    draft = {r[0] for r in cur.fetchall()}
    cur.execute(PUBLISHED_SET_SQL)
    published = {r[0] for r in cur.fetchall()}
    diff = draft ^ published
    if diff:
        print(f"[dedupe] 拒绝提交：草稿与发布版本工具集不一致 → {sorted(diff)}")
        print(f"[dedupe]   草稿 {sorted(draft)}")
        print(f"[dedupe]   发布 {sorted(published)}")
        conn.rollback()
        return 1

    conn.commit()

    cur.execute(DUP_SQL)
    remaining = cur.fetchall()
    if remaining:
        print(f"[dedupe] 清完仍有重复：{remaining}")
        return 1
    print("[dedupe] 清完复检：无重复")
    return 0


if __name__ == "__main__":
    sys.exit(main())
