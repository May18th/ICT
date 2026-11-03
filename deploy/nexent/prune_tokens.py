"""清理临时北向 API Key（软删除，走平台自己的 `delete_token`）。

为什么需要这个脚本
------------------
`setup_nexent.py mint-key` 每跑一次就新增一个 Key，验收时会造出一串"一次性 Key"。
这些 Key 留在库里等于长期有效的凭据：谁拿到 `user_token_info_t` 里的明文就能调北向接口。
验收结束后应当按"创建时间 + 保留清单"成批清掉，而不是让人翻 UI 一个个点。

默认是 **dry-run**：只打印将被删除的 Key（只显示前 12 位），加 `--apply` 才真的删。
删除是软删（`delete_flag='Y'`），与平台 UI 删除行为一致，可追溯、不物理丢行。

用法（宿主机执行）：

    docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/prune_tokens.py    nexent-config:/tmp/prune_tokens.py
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/prune_tokens.py \
        --created-after '2026-11-03 00:00' --keep 3,4"
    # 确认清单无误后加 --apply 真删

退出码：0 完成（含无可删）；1 参数错误或删除失败。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if "/opt/backend" not in sys.path:
    sys.path.insert(0, "/opt/backend")

from platform_client import admin_user  # noqa: E402


def _load_rows(user_id: str) -> list[dict]:
    from database.client import get_db_session  # type: ignore
    from database.db_models import UserTokenInfo  # type: ignore

    with get_db_session() as session:
        rows = (
            session.query(UserTokenInfo)
            .filter(UserTokenInfo.user_id == user_id, UserTokenInfo.delete_flag == "N")
            .order_by(UserTokenInfo.token_id.asc())
            .all()
        )
        return [
            {
                "token_id": row.token_id,
                "access_key": row.access_key,
                "create_time": row.create_time,
            }
            for row in rows
        ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="按创建时间清理临时北向 API Key（默认 dry-run）")
    parser.add_argument("--created-after", default=None, help="只处理此时间之后创建的 Key，格式 'YYYY-MM-DD HH:MM'")
    parser.add_argument("--keep", default="", help="保留的 token_id，逗号分隔（例如 3,4）")
    parser.add_argument("--apply", action="store_true", help="真的执行软删除；不加则只打印清单")
    args = parser.parse_args(argv[1:])

    keep = {int(item) for item in args.keep.split(",") if item.strip().isdigit()}
    cutoff = None
    if args.created_after:
        try:
            cutoff = datetime.strptime(args.created_after, "%Y-%m-%d %H:%M")
        except ValueError:
            print(f"[prune] --created-after 格式应为 'YYYY-MM-DD HH:MM'，收到 {args.created_after!r}", file=sys.stderr)
            return 1

    user_id = admin_user()
    rows = _load_rows(user_id)
    targets = []
    for row in rows:
        if row["token_id"] in keep:
            continue
        if cutoff is not None and row["create_time"] and row["create_time"] < cutoff:
            continue
        targets.append(row)

    print(f"[prune] 现存 {len(rows)} 个有效 Key；保留清单 {sorted(keep) or '（空）'}；命中待清理 {len(targets)} 个")
    for row in targets:
        print(f"  - token_id={row['token_id']} create_time={row['create_time']} key={row['access_key'][:12]}…")

    if not args.apply:
        print("[prune] dry-run：未做任何改动；确认清单后加 --apply 执行软删除")
        return 0

    from database.token_db import delete_token  # type: ignore

    failed = 0
    for row in targets:
        ok = delete_token(row["token_id"], user_id)
        print(f"[prune] delete token_id={row['token_id']}: {'ok' if ok else 'failed'}")
        if not ok:
            failed += 1
    remaining = len(_load_rows(user_id))
    print(f"[prune] 完成：删除 {len(targets) - failed} 个，失败 {failed} 个；现存有效 Key {remaining} 个")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
