"""离线补传批次：接收、逐行隔离、完成与更正重放。

批次生命周期 receiving -> completed。接收时只落原始行和结构校验，
完成时在单个事务内按来源修订号合并；每行必落到生效/过旧/重复/隔离之一，
完成时强制校验 输入条数 = 生效+过旧+重复+隔离。
"""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError,OperationalError
from src.models.entities import ArrivalBatch,ArrivalBatchItem
from src.services.ledger import (APPLIED,STALE,DUPLICATE,QUARANTINED,TERMINAL_STATES,RowError,
 normalize_row,identity,apply_offline,lock_identity)

BATCH_RECEIVING='receiving'
BATCH_COMPLETED='completed'
COUNT_FIELDS=(APPLIED,STALE,DUPLICATE,QUARANTINED)

def payload_hash(source,rows):
 canonical=json.dumps({'source':source,'rows':rows},sort_keys=True,ensure_ascii=False,separators=(',',':'),default=str)
 return hashlib.sha256(canonical.encode()).hexdigest()

def item_payload(item):
 return {'id':item.id,'batch_id':item.batch_id,'row_no':item.row_no,'raw':item.raw,'status':item.status,
  'reason':item.reason or '','arrival_id':item.arrival_id,'station':item.station,'code':item.code,
  'planned_day':item.planned_day,'source_revision':item.source_revision,'revoked_request':item.revoked_request,
  'replay_of_item_id':item.replay_of_item_id,'resolved_by_item_id':item.resolved_by_item_id,
  'resolution_status':item.resolution_status,'resolved_at':item.resolved_at,
  'processed_at':item.processed_at}

def batch_payload(batch,items=None):
 data={'id':batch.id,'batch_key':batch.batch_key,'source':batch.source,'status':batch.status,
  'input_count':batch.input_count,'applied_count':batch.applied_count,'stale_count':batch.stale_count,
  'duplicate_count':batch.duplicate_count,'quarantined_count':batch.quarantined_count,
  'created_by':batch.created_by,'created_at':batch.created_at,'completed_at':batch.completed_at,
  'payload_hash':batch.payload_hash,
  'balanced':batch.input_count==batch.applied_count+batch.stale_count+batch.duplicate_count+batch.quarantined_count}
 if items is not None:data['items']=[item_payload(i) for i in items]
 return data

def get_batch(s,i,with_items=True):
 batch=s.get(ArrivalBatch,i)
 if not batch:raise HTTPException(404,'补传批次不存在')
 items=s.query(ArrivalBatchItem).filter_by(batch_id=i).order_by(ArrivalBatchItem.row_no).all() if with_items else None
 return batch,items

def receive_batch(s,data,actor,now):
 """接收批次。同批次编号同内容重试直接返回已有批次，不重复写入。"""
 digest=payload_hash(data.source,[r for r in data.rows])
 existing=s.query(ArrivalBatch).filter_by(batch_key=data.batch_key).with_for_update().first()
 if existing:
  if existing.payload_hash!=digest:raise HTTPException(409,'批次编号已被不同内容使用，请换用新的批次编号')
  return existing,False
 batch=ArrivalBatch(batch_key=data.batch_key,source=data.source,status=BATCH_RECEIVING,input_count=len(data.rows),
  payload_hash=digest,created_by=actor,created_at=now)
 s.add(batch)
 try:s.flush()
 except IntegrityError:
  s.rollback();existing=s.query(ArrivalBatch).filter_by(batch_key=data.batch_key).first()
  if existing and existing.payload_hash==digest:return existing,False
  raise HTTPException(409,'批次编号并发冲突，请重试')
 for row_no,raw in enumerate(data.rows,start=1):
  item=ArrivalBatchItem(batch_id=batch.id,row_no=row_no,raw=raw)
  try:
   normalized=normalize_row(raw)
   item.normalized=normalized;item.station=normalized['station'];item.code=normalized['code']
   item.planned_day=normalized['planned_day'];item.source_revision=normalized['revision']
   item.revoked_request=normalized['revoked']
  except RowError as exc:
   item.status=QUARANTINED;item.reason=str(exc);item.processed_at=now
  s.add(item)
 s.commit()
 return batch,True

def _counts(items):
 counts={name:0 for name in COUNT_FIELDS}
 for item in items:
  if item.status not in counts:raise HTTPException(500,f'批次行#{item.id}处于未知状态 {item.status}')
  counts[item.status]+=1
 return counts

def _apply_one(s,item,normalized,batch,now):
 key=identity(normalized);lock_identity(s,key)
 for attempt in range(3):
  try:
   with s.begin_nested():
    status,reason,arrival_id=apply_offline(s,normalized,now,batch.id,item.id,batch.source)
   return status,reason,arrival_id
  except IntegrityError:
   if attempt==2:raise HTTPException(409,'并发写入同一班次冲突，请重试完成该批次')
   s.expire_all()
  except OperationalError:
   # 死锁/序列化失败会中止整个事务；询问调用方重试整批，而不是把行错记为隔离。
   raise HTTPException(409,'并发写入同一班次被数据库中止，请重试完成该批次')

def complete_batch(s,batch_id,now,commit=True):
 """完成批次：逐行合并，汇总四类结果并校验条数平衡。重复调用不再重复写入。

 commit=False 供重放在同一事务内完成，避免行锁在中途释放。
 """
 batch=s.query(ArrivalBatch).filter_by(id=batch_id).with_for_update().first()
 if not batch:raise HTTPException(404,'补传批次不存在')
 items=s.query(ArrivalBatchItem).filter_by(batch_id=batch.id).order_by(ArrivalBatchItem.row_no).all()
 if batch.status==BATCH_COMPLETED:return batch,items,False
 pending=[]
 for item in items:
  if item.status in TERMINAL_STATES:continue
  if item.normalized is None:
   try:item.normalized=normalize_row(item.raw)
   except RowError as exc:
    item.status=QUARANTINED;item.reason=str(exc);item.processed_at=now;continue
  pending.append(item)
 # 同一班次按来源修订号降序合并：乱序到达也先取最高修订，低修订自然记为过旧。
 pending.sort(key=lambda i:(identity(i.normalized),-i.normalized['revision'],i.row_no))
 for item in pending:
  status,reason,arrival_id=_apply_one(s,item,item.normalized,batch,now)
  item.status=status;item.reason=reason;item.arrival_id=arrival_id;item.processed_at=now
 counts=_counts(items)
 batch.applied_count=counts[APPLIED];batch.stale_count=counts[STALE]
 batch.duplicate_count=counts[DUPLICATE];batch.quarantined_count=counts[QUARANTINED]
 total=sum(counts.values())
 if total!=batch.input_count or len(items)!=batch.input_count:
  s.rollback();raise HTTPException(500,f'批次条数不平：输入{batch.input_count}，结果合计{total}')
 batch.status=BATCH_COMPLETED;batch.completed_at=now
 if commit:s.commit()
 else:s.flush()
 return batch,items,True

def replay_item(s,item_id,row,actor,now,batch_key=None):
 """用更正后的行重放隔离项，并回填原行的处理结果以便追溯。"""
 item=s.query(ArrivalBatchItem).filter_by(id=item_id).with_for_update().first()
 if not item:raise HTTPException(404,'隔离行不存在')
 original=s.get(ArrivalBatch,item.batch_id)
 if not original:raise HTTPException(404,'原批次不存在')
 if original.status!=BATCH_COMPLETED:raise HTTPException(409,'原批次尚未完成，不能重放隔离项')
 if item.status!=QUARANTINED:raise HTTPException(409,'只有隔离行可以更正重放')
 if item.resolved_by_item_id is not None:
  existing_replay=s.get(ArrivalBatchItem,item.resolved_by_item_id)
  existing_batch=s.get(ArrivalBatch,existing_replay.batch_id) if existing_replay else None
  if existing_batch and batch_key and existing_batch.batch_key==batch_key:
   return existing_batch,existing_replay,item
  raise HTTPException(409,f'隔离行已由行#{item.resolved_by_item_id}处理（{item.resolution_status}），不再重复重放')
 stamp=now.replace(':','').replace('-','').replace('+00:00','').replace('.','')[:20]
 key=batch_key or f'replay-{original.id}-{item.id}-{stamp}'
 digest=payload_hash('replay',[row])
 replay_batch=s.query(ArrivalBatch).filter_by(batch_key=key).with_for_update().first()
 replay_row=None
 if replay_batch:
  if replay_batch.payload_hash!=digest:raise HTTPException(409,'重放批次编号已被不同内容使用')
  if replay_batch.status!=BATCH_COMPLETED:
   replay_batch,items,_=complete_batch(s,replay_batch.id,now,commit=False)
   replay_row=next((x for x in items if x.replay_of_item_id==item.id),None)
 else:
  replay_batch=ArrivalBatch(batch_key=key,source='replay',status=BATCH_RECEIVING,input_count=1,
   payload_hash=digest,created_by=actor,created_at=now)
  s.add(replay_batch);s.flush()
  replay_row=ArrivalBatchItem(batch_id=replay_batch.id,row_no=1,raw=row,replay_of_item_id=item.id)
  try:
   normalized=normalize_row(row)
   replay_row.normalized=normalized;replay_row.station=normalized['station']
   replay_row.code=normalized['code'];replay_row.planned_day=normalized['planned_day']
   replay_row.source_revision=normalized['revision'];replay_row.revoked_request=normalized['revoked']
  except RowError as exc:
   replay_row.status=QUARANTINED;replay_row.reason=str(exc);replay_row.processed_at=now
  s.add(replay_row);s.flush()
  replay_batch,items,_=complete_batch(s,replay_batch.id,now,commit=False)
  replay_row=next((x for x in items if x.replay_of_item_id==item.id),None)
 item.resolved_by_item_id=replay_row.id if replay_row else None
 item.resolution_status=replay_row.status if replay_row else QUARANTINED
 item.resolved_at=now
 s.commit()
 return replay_batch,replay_row,item

def quarantine_list(s):
 rows=s.query(ArrivalBatchItem).filter(ArrivalBatchItem.status==QUARANTINED).order_by(ArrivalBatchItem.id.desc()).limit(500).all()
 batch_ids={r.batch_id for r in rows}
 batches={b.id:b for b in s.query(ArrivalBatch).filter(ArrivalBatch.id.in_(batch_ids)).all()} if batch_ids else {}
 out=[]
 for r in rows:
  data=item_payload(r);batch=batches.get(r.batch_id)
  data['batch_key']=batch.batch_key if batch else None;data['batch_status']=batch.status if batch else None
  out.append(data)
 return out

def trace_item(s,item_id):
 """隔离项追溯：原行、原始批次、重放批次与最终处理结果。"""
 item=s.query(ArrivalBatchItem).filter_by(id=item_id).first()
 if not item:raise HTTPException(404,'批次行不存在')
 batch=s.get(ArrivalBatch,item.batch_id)
 replays=s.query(ArrivalBatchItem).filter_by(replay_of_item_id=item.id).order_by(ArrivalBatchItem.id).all()
 def with_batch(row):
  b=s.get(ArrivalBatch,row.batch_id)
  data=item_payload(row);data['batch_key']=b.batch_key if b else None;data['batch_status']=b.status if b else None
  return data
 resolved=s.get(ArrivalBatchItem,item.resolved_by_item_id) if item.resolved_by_item_id else None
 return {'item':with_batch(item),'batch':batch_payload(batch) if batch else None,
  'replays':[with_batch(r) for r in replays],
  'resolved_by':with_batch(resolved) if resolved else None,
  'resolution_status':item.resolution_status,'resolved_at':item.resolved_at}
