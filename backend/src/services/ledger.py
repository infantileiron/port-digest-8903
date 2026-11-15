"""到发台账的身份、规范化与修订合并规则。

班次身份 = 港站 + 班次编号 + 上海计划日。离线补传按来源修订号合并：
更高的修订生效，更低的过旧，同号同版本同内容重复，同号同版本异内容隔离。
撤销写入墓碑水印；引用不高于撤销修订号的旧数据不得复活班次。
"""
import hashlib
import re
from datetime import timezone
from sqlalchemy import text
from src.constants.enums import STATIONS
from src.models.entities import Arrival,ArrivalTombstone
from src.utils.time import aware,SH

APPLIED='applied'
STALE='stale'
DUPLICATE='duplicate'
QUARANTINED='quarantined'
TERMINAL_STATES=(APPLIED,STALE,DUPLICATE,QUARANTINED)

class RowError(ValueError):
 pass

def _utc(value):
 return aware(value).astimezone(timezone.utc).isoformat()

def normalize_row(raw):
 """规范化为可比较的业务内容；坏行抛出带原因的 RowError，不产生副作用。"""
 if not isinstance(raw,dict):raise RowError('行必须是JSON对象')
 station=raw.get('station')
 if station not in STATIONS:raise RowError('港站无效，只能是：'+'、'.join(STATIONS))
 code=raw.get('code')
 if not isinstance(code,str) or not code.strip():raise RowError('班次编号不能为空')
 code=code.strip()
 if len(code)>60:raise RowError('班次编号超过60个字符')
 planned_raw=raw.get('planned_at')
 if planned_raw in (None,''):raise RowError('计划时间不能为空')
 try:planned=_utc(planned_raw)
 except Exception:raise RowError('计划时间必须包含时区偏移，例如2026-11-14T23:55:00+08:00')
 actual_raw=raw.get('actual_at')
 try:actual=_utc(actual_raw) if actual_raw not in (None,'') else None
 except Exception:raise RowError('实际时间必须包含时区偏移，例如2026-11-15T00:05:00+08:00')
 if actual and abs((aware(actual)-aware(planned)).total_seconds())>3*86400:raise RowError('实际与计划时间相差超过3天，请核对')
 note=raw.get('note','')
 if note is None:note=''
 if not isinstance(note,str):raise RowError('交接说明必须是文本')
 if len(note)>1000:raise RowError('交接说明超过1000个字符')
 revision=raw.get('revision',0)
 if isinstance(revision,str) and re.fullmatch(r'\d{1,9}',revision):revision=int(revision)
 if isinstance(revision,bool) or not isinstance(revision,int) or revision<0:raise RowError('来源修订号必须是非负整数')
 revoked=raw.get('revoked',False)
 if isinstance(revoked,int) and revoked in (0,1):revoked=bool(revoked)
 if not isinstance(revoked,bool):raise RowError('撤销标记必须是布尔值')
 return {'code':code,'station':station,'planned_at':planned,'planned_day':aware(planned).astimezone(SH).date().isoformat(),
  'actual_at':actual,'note':note,'revision':revision,'revoked':revoked}

def identity(normalized):
 return (normalized['station'],normalized['code'],normalized['planned_day'])

def canonical_text(station,code,planned,actual,note,revoked):
 """稳定的内容指纹文本（UTC时间、上海计划日），离线行与人工修订使用同一算法。"""
 return 'v1|'+'|'.join([station or '',code or '',aware(planned).astimezone(timezone.utc).isoformat(),
  aware(planned).astimezone(SH).date().isoformat(),aware(actual).astimezone(timezone.utc).isoformat() if actual else '',
  note or '','1' if revoked else '0'])

def content_hash(normalized):
 return hashlib.sha256(canonical_text(normalized['station'],normalized['code'],normalized['planned_at'],
  normalized['actual_at'],normalized['note'],normalized['revoked']).encode()).hexdigest()

def manual_hash(station,code,planned_at,actual_at,note,revoked=False):
 return hashlib.sha256(canonical_text(station,code,planned_at,actual_at,note,revoked).encode()).hexdigest()

def revision_of(record):
 return int(record.source_revision or 0)

def _label(key):
 return f'{key[0]}/{key[1]}/{key[2]}'

def lock_identity(s,key):
 """PostgreSQL 下按班次身份取事务级 advisory lock，串行化离线批次与人工修订。"""
 if s.get_bind().dialect.name!='postgresql':return
 h1=int.from_bytes(hashlib.blake2b(('s:'+key[0]+'|'+key[1]).encode(),digest_size=4).digest(),'big',signed=True)
 h2=int.from_bytes(hashlib.blake2b(('d:'+key[2]).encode(),digest_size=4).digest(),'big',signed=True)
 s.execute(text('SELECT pg_advisory_xact_lock(:a,:b)'),{'a':h1,'b':h2})

def _locked(s,model,key):
 return s.query(model).filter_by(station=key[0],code=key[1],planned_day=key[2]).with_for_update().first()

def _conflict(key,revision,arrival_id=None):
 return QUARANTINED,f'同号同版本异内容：{_label(key)} 修订{revision}已存在且内容不同',arrival_id

def _touch(arrival,normalized,revision,digest,source,batch_id,now):
 arrival.code=normalized['code'];arrival.station=normalized['station'];arrival.planned_at=normalized['planned_at']
 arrival.planned_day=normalized['planned_day'];arrival.actual_at=normalized['actual_at'];arrival.note=normalized['note']
 arrival.source_revision=revision;arrival.source_content=digest;arrival.source=source
 arrival.last_batch_id=batch_id;arrival.updated_at=now

def _revoke_watermark(s,normalized,revision,digest,source,batch_id,item_id,now):
 key=identity(normalized);tomb=_locked(s,ArrivalTombstone,key)
 if not tomb:
  tomb=ArrivalTombstone(station=key[0],code=key[1],planned_day=key[2]);s.add(tomb)
 tomb.revoked_revision=revision;tomb.revoked_at=now;tomb.source=source
 tomb.content_hash=digest;tomb.last_batch_id=batch_id;tomb.last_item_id=item_id
 s.flush()
 return tomb

def apply_offline(s,normalized,now,batch_id=None,item_id=None,source='offline'):
 """在调用方事务内合并一行离线修订，返回 (状态, 原因, arrival_id)。"""
 key=identity(normalized);revision=normalized['revision'];digest=content_hash(normalized)
 arrival=_locked(s,Arrival,key);tomb=_locked(s,ArrivalTombstone,key)
 watermark=int(tomb.revoked_revision) if tomb else None

 if normalized['revoked']:
  if arrival and not arrival.revoked:
   current=revision_of(arrival)
   if revision<current:return STALE,f'过旧修订：撤销修订{revision}低于台账修订{current}',arrival.id
   if revision==current:return _conflict(key,revision,arrival.id if arrival else None)
   arrival.revoked=True;arrival.revoked_revision=revision;arrival.revoked_at=now;arrival.updated_at=now
   arrival.source=source;arrival.last_batch_id=batch_id
   _revoke_watermark(s,normalized,revision,digest,source,batch_id,item_id,now)
   return APPLIED,f'撤销修订{revision}生效',arrival.id
  if watermark is not None and revision<watermark:
   return STALE,f'过旧修订：撤销修订{revision}低于已记录撤销{watermark}',arrival.id if arrival else None
  if watermark is not None and revision==watermark:
   if digest==tomb.content_hash:return DUPLICATE,'同修订同内容，撤销已记录',arrival.id if arrival else None
   return _conflict(key,revision,arrival.id if arrival else None)
  if arrival:
   arrival.revoked=True;arrival.revoked_revision=revision;arrival.revoked_at=now;arrival.updated_at=now
   arrival.source=source;arrival.last_batch_id=batch_id
  _revoke_watermark(s,normalized,revision,digest,source,batch_id,item_id,now)
  return APPLIED,f'撤销修订{revision}生效',arrival.id if arrival else None

 if arrival and not arrival.revoked:
  current=revision_of(arrival)
  if revision<current:return STALE,f'过旧修订：修订{revision}低于台账修订{current}',arrival.id
  if revision==current:
   if arrival.source_content is None:
    return QUARANTINED,f'台账缺少内容指纹，无法判定{_label(key)} 修订{revision}是否同内容，请用更高修订覆盖',arrival.id
   if digest==arrival.source_content:return DUPLICATE,'同修订同内容，未重复写入',arrival.id
   return _conflict(key,revision,arrival.id if arrival else None)
  _touch(arrival,normalized,revision,digest,source,batch_id,now)
  return APPLIED,f'修订{revision}生效',arrival.id
 if arrival and arrival.revoked:
  if watermark is not None and revision==watermark:
   if digest==tomb.content_hash:return DUPLICATE,'同修订同内容，撤销已记录',arrival.id
   return _conflict(key,revision,arrival.id)
  if watermark is not None and revision<watermark:
   return STALE,f'撤销后旧修订不得复活：修订{revision}低于撤销修订{watermark}',arrival.id
  if watermark is None and revision<=revision_of(arrival):
   return STALE,f'过旧修订：修订{revision}不高于撤销修订{revision_of(arrival)}',arrival.id
  arrival.revoked=False;arrival.revoked_revision=None;arrival.revoked_at=None
  _touch(arrival,normalized,revision,digest,source,batch_id,now)
  return APPLIED,f'修订{revision}高于撤销修订，班次恢复',arrival.id
 if watermark is not None and revision==watermark:
  if digest==tomb.content_hash:return DUPLICATE,'同修订同内容，撤销已记录',None
  return _conflict(key,revision,None)
 if watermark is not None and revision<watermark:
  return STALE,f'撤销后旧修订不得复活：修订{revision}低于撤销修订{watermark}',None
 arrival=Arrival(code=normalized['code'],station=normalized['station'],planned_at=normalized['planned_at'],
  planned_day=normalized['planned_day'],actual_at=normalized['actual_at'],note=normalized['note'],
  source_revision=revision,source_content=digest,revoked=False,source=source,last_batch_id=batch_id,updated_at=now)
 s.add(arrival);s.flush()
 return APPLIED,f'修订{revision}生效',arrival.id
