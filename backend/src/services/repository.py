from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.models.entities import Arrival,Subscription,Receipt,ArrivalTombstone
from src.services.business import dump
from src.services.ledger import manual_hash,lock_identity
from src.utils.time import now_utc,next_due,aware,SH

def _manual_revision(arrival):
 """人工编辑总是产生高于当前修订和撤销修订的新修订，因而既能覆盖旧补传，也能显式恢复撤销班次。"""
 return max(int(arrival.source_revision or 0),int(arrival.revoked_revision or 0))+1


ARRIVAL_FIELDS=('id','code','station','planned_at','planned_day','actual_at','note','source_revision','source','revoked','revoked_revision')

def arrival_payload(r):return {k:getattr(r,k) for k in ARRIVAL_FIELDS}

def arrival_rows(s,include_revoked=False):
 q=s.query(Arrival)
 if not include_revoked:q=q.filter(Arrival.revoked.is_(False))
 return [arrival_payload(r) for r in q.order_by(Arrival.planned_at,Arrival.id).all()]

def save_arrival(s,data,i=None):
 values=data.model_dump()
 planned_day=aware(data.planned_at).astimezone(SH).date().isoformat()
 new_key=(data.station,data.code,planned_day)
 old_key=None
 if i:
  probe=s.get(Arrival,i)
  if not probe:raise HTTPException(404,'到发记录不存在')
  old_key=(probe.station,probe.code,probe.planned_day)
 # 先按固定顺序取身份级锁，再取行锁：与离线批次同一加锁顺序，避免互锁。
 for key in sorted({new_key} if old_key is None else {old_key,new_key}):
  lock_identity(s,key)
 if i:
  r=s.query(Arrival).filter_by(id=i).with_for_update().first()
  if not r:raise HTTPException(404,'到发记录不存在')
 else:
  r=Arrival()
  tomb=s.query(ArrivalTombstone).filter_by(station=new_key[0],code=new_key[1],planned_day=new_key[2]).first()
  if tomb:raise HTTPException(409,f'班次已被撤销（撤销修订{tomb.revoked_revision}），旧数据不能复活；请通过补传更高修订恢复')
 if old_key is not None and old_key!=new_key:
  taken=s.query(Arrival).filter_by(station=new_key[0],code=new_key[1],planned_day=new_key[2]).first()
  if taken and taken.id!=r.id:raise HTTPException(409,'目标港站/编号/计划日已有班次记录')
  tomb=s.query(ArrivalTombstone).filter_by(station=new_key[0],code=new_key[1],planned_day=new_key[2]).first()
  if tomb:raise HTTPException(409,f'目标班次已被撤销（撤销修订{tomb.revoked_revision}），不能移入')

 for k,v in values.items():setattr(r,k,v)
 r.planned_day=planned_day
 # 人工录入/修订也占一个修订号：内容一致可去重，内容不同则形成新的合并基线。
 r.source_revision=1 if not i else _manual_revision(r)
 r.revoked=False;r.revoked_revision=None;r.revoked_at=None;r.source='manual'
 r.source_content=manual_hash(r.station,r.code,r.planned_at,r.actual_at,r.note)
 r.updated_at=now_utc().isoformat()
 if old_key is not None and old_key!=new_key:
  # 身份迁移后，旧身份以同一修订号立墓碑：更低修订的旧补传不能把原班次带回来。
  tomb=s.query(ArrivalTombstone).filter_by(station=old_key[0],code=old_key[1],planned_day=old_key[2]).first()
  if not tomb:
   tomb=ArrivalTombstone(station=old_key[0],code=old_key[1],planned_day=old_key[2]);s.add(tomb)
  tomb.revoked_revision=r.source_revision;tomb.revoked_at=r.updated_at;tomb.source='manual'
  tomb.content_hash=None
 s.add(r)
 try:s.commit()
 except IntegrityError:
  s.rollback();raise HTTPException(409,'同港站同计划日已有该班次编号')
 return r

def save_subscription(s,data,i=None):
 r=s.get(Subscription,i) if i else Subscription();new=i is None
 if not r:raise HTTPException(404,'订阅不存在')
 values=data.model_dump();first=values.pop('first_due_at')
 for k,v in values.items():setattr(r,k,v)
 # Explicit edit schedules the next future run; an initial override is for a first/catch-up delivery.
 r.next_run_at=first or next_due(r.daily_at,now_utc()).isoformat()
 s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'订阅名称重复')
 return r

def get_receipt(s,i):
 r=s.get(Receipt,i)
 if not r:raise HTTPException(404,'回执不存在')
 return r
