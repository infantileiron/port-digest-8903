"""幂等数据库迁移执行器。

按文件名顺序执行 migrations/*.sql。001 是历史基线：库中已有 arrivals 时跳过，
其余脚本全部使用 IF NOT EXISTS / 条件约束，可重复执行。
"""
import re
import sys
from pathlib import Path
from sqlalchemy import text
from src.config.database import engine

MIGRATIONS=Path(__file__).resolve().parent.parent/'migrations'

def _statements(path):
 sql=path.read_text()
 # psql 元命令（\restrict / \unrestrict）不能通过驱动执行。
 return '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('\\'))

def _has_arrivals(conn):
 return conn.execute(text("SELECT to_regclass('public.arrivals') IS NOT NULL")).scalar()

def apply():
 with engine.begin() as conn:
  for path in sorted(MIGRATIONS.glob('*.sql')):
   baseline=re.match(r'001',path.name)
   if baseline and _has_arrivals(conn):
    print(f'skip {path.name}（基线已存在）');continue
   conn.exec_driver_sql(_statements(path))
   print(f'applied {path.name}')
 return True

if __name__=='__main__':
 try:apply()
 except Exception as exc:
  print('迁移失败：',exc,file=sys.stderr);raise
