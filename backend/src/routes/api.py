from fastapi import APIRouter,Depends,HTTPException,Query
from src.config.database import db
from src.models.entities import User,Subscription,Receipt,Audit
from src.schemas.business import Login,ArrivalInput,BatchInput,ReplayInput,SubscriptionInput,PreviewInput,ReviewInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.utils.time import now_utc,yesterday
from src.services.business import audit,dump
from src.services.repository import arrival_rows,arrival_payload,save_arrival,save_subscription,get_receipt
from src.services.digest import calculate
from src.services.batch import (receive_batch,complete_batch,get_batch,batch_payload,replay_item,
 quarantine_list,trace_item,item_payload)
from src.controllers.business import export_receipt
router=APIRouter()

@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/context')
def context(u=Depends(current)):
 now=now_utc();return {'now':now.isoformat(),'yesterday':yesterday(now),'timezone':'Asia/Shanghai'}
@router.get('/arrivals')
def arrivals(include_revoked:bool=Query(False),u=Depends(current),s=Depends(db)):
 if include_revoked and u.role!='admin':raise HTTPException(403,'仅负责人可查看撤销记录')
 return arrival_rows(s,include_revoked=include_revoked)
@router.post('/arrivals')
def create_arrival(data:ArrivalInput,u=Depends(editor),s=Depends(db)):
 r=save_arrival(s,data);audit(s,u,'录入班次 '+r.code);return arrival_payload(r)
@router.put('/arrivals/{i}')
def edit_arrival(i:int,data:ArrivalInput,u=Depends(editor),s=Depends(db)):
 r=save_arrival(s,data,i);audit(s,u,'修订班次 '+r.code);return arrival_payload(r)

@router.post('/arrival-batches')
def create_batch(data:BatchInput,u=Depends(editor),s=Depends(db)):
 now=now_utc().isoformat();batch,created=receive_batch(s,data,u.email,now)
 if created:audit(s,u,f'接收补传批次 {batch.batch_key}（{batch.input_count}行）')
 _,items=get_batch(s,batch.id)
 return {**batch_payload(batch,items),'created':created}
@router.get('/arrival-batches')
def list_batches(u=Depends(current),s=Depends(db)):
 from src.models.entities import ArrivalBatch
 return [batch_payload(b) for b in s.query(ArrivalBatch).order_by(ArrivalBatch.id.desc()).all()]
@router.post('/arrival-batches/{i}/complete')
def finish_batch(i:int,u=Depends(editor),s=Depends(db)):
 batch,items,processed=complete_batch(s,i,now_utc().isoformat())
 if processed:audit(s,u,f'完成补传批次 {batch.batch_key}：生效{batch.applied_count} 过旧{batch.stale_count} 重复{batch.duplicate_count} 隔离{batch.quarantined_count}')
 return {**batch_payload(batch,items),'processed':processed}
@router.get('/arrival-batches/{i}')
def read_batch(i:int,u=Depends(current),s=Depends(db)):
 batch,items=get_batch(s,i)
 return batch_payload(batch,items)
@router.get('/arrival-quarantine')
def quarantine(u=Depends(current),s=Depends(db)):return quarantine_list(s)
@router.post('/arrival-batch-items/{i}/replay')
def replay(i:int,data:ReplayInput,u=Depends(editor),s=Depends(db)):
 batch,row,item=replay_item(s,i,data.row,u.email,now_utc().isoformat(),data.batch_key)
 audit(s,u,f'重放隔离行 {i}：{row.status if row else "未生成"}')
 return {'batch':batch_payload(batch,[row] if row else []),'item':item_payload(item) if item else None,
  'original':trace_item(s,i)}
@router.get('/arrival-batch-items/{i}/trace')
def trace(i:int,u=Depends(current),s=Depends(db)):return trace_item(s,i)

@router.post('/preview')
def preview(data:PreviewInput,u=Depends(editor),s=Depends(db)):
 result=calculate(arrival_rows(s),data.report_day.isoformat(),data.station);audit(s,u,'晨报试算 '+data.report_day.isoformat());return result
@router.get('/subscriptions')
def subscriptions(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Subscription).order_by(Subscription.id).all()]
@router.post('/subscriptions')
def create_subscription(data:SubscriptionInput,u=Depends(admin),s=Depends(db)):
 r=save_subscription(s,data);audit(s,u,'创建订阅 '+r.name);return dump(r)
@router.put('/subscriptions/{i}')
def edit_subscription(i:int,data:SubscriptionInput,u=Depends(admin),s=Depends(db)):
 r=save_subscription(s,data,i);audit(s,u,'修订订阅 '+r.name);return dump(r)
@router.get('/receipts')
def receipts(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Receipt).order_by(Receipt.id.desc()).all()]
@router.get('/receipts/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):return export_receipt(s,i)
@router.post('/receipts/{i}/review')
def review(i:int,data:ReviewInput,u=Depends(admin),s=Depends(db)):
 r=get_receipt(s,i)
 if r.state=='reviewed':raise HTTPException(409,'回执已复核，不覆盖原复核意见')
 r.state='reviewed';r.review_note=data.note;s.commit();audit(s,u,'复核晨报回执 '+str(i));return dump(r)
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]
