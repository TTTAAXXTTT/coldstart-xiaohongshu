"""Interactive creator login, called after the managed workbench has stopped."""
import argparse
import json
import sys
import ops
import validation
import xhs_session


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',required=True)
    args=parser.parse_args()
    try:
        with validation.InstanceLock(args.data_dir):
            with ops.connection(args.data_dir,True) as db:
                row=db.execute("SELECT value FROM meta WHERE key='xhs_bound_user'").fetchone()
            print('请在打开的 Chromium 窗口完成主站和创作者中心登录。成功后自动保存并关闭。',flush=True)
            report=xhs_session.invoke(row[0] if row else '',mode='login')
            print(json.dumps({'ok':True,'checked_at':report['checked_at'],'detail':report['detail']},ensure_ascii=False))
        return 0
    except Exception as error:
        print(str(error),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
