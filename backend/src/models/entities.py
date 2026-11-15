from sqlalchemy import Column,Integer,String,JSON,Text,Boolean,ForeignKey,UniqueConstraint,Index
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True,nullable=False);password=Column(String(200));role=Column(String(20))
class Arrival(Base):
 __tablename__='arrivals'
 __table_args__=(
  UniqueConstraint('station','code','planned_day',name='uq_arrival_station_code_day'),
  Index('ix_arrivals_planned_day','planned_day'),
 )
 id=Column(Integer,primary_key=True);code=Column(String(60),nullable=False);station=Column(String(30));planned_at=Column(String(40));planned_day=Column(String(10),nullable=False)
 actual_at=Column(String(40),nullable=True);note=Column(Text,default='')
 source_revision=Column(Integer,nullable=False,default=0)
 source_content=Column(String(64),nullable=True)
 revoked=Column(Boolean,nullable=False,default=False)
 revoked_revision=Column(Integer,nullable=True)
 revoked_at=Column(String(40),nullable=True)
 source=Column(String(40),default='manual')
 last_batch_id=Column(Integer,nullable=True)
 updated_at=Column(String(40),nullable=True)
class Subscription(Base):
 __tablename__='subscriptions'
 id=Column(Integer,primary_key=True);name=Column(String(100),unique=True,nullable=False);station=Column(String(30));daily_at=Column(String(5));recipient=Column(String(80));enabled=Column(Boolean,default=False);next_run_at=Column(String(40));last_run_at=Column(String(40),nullable=True)
class Receipt(Base):
 __tablename__='receipts'
 __table_args__=(UniqueConstraint('subscription_id','report_day',name='uq_receipt_subscription_day'),)
 id=Column(Integer,primary_key=True);subscription_id=Column(Integer,ForeignKey('subscriptions.id'));report_day=Column(String(10));recipient=Column(String(80));created_at=Column(String(40));state=Column(String(20),default='delivered');payload=Column(JSON);review_note=Column(Text,default='')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
class ArrivalBatch(Base):
 __tablename__='arrival_batches'
 id=Column(Integer,primary_key=True)
 batch_key=Column(String(100),unique=True,nullable=False)
 source=Column(String(80),default='offline')
 status=Column(String(20),nullable=False,default='receiving')
 input_count=Column(Integer,nullable=False,default=0)
 applied_count=Column(Integer,nullable=False,default=0)
 stale_count=Column(Integer,nullable=False,default=0)
 duplicate_count=Column(Integer,nullable=False,default=0)
 quarantined_count=Column(Integer,nullable=False,default=0)
 payload_hash=Column(String(64),nullable=False)
 created_by=Column(String(100))
 created_at=Column(String(40))
 completed_at=Column(String(40),nullable=True)
class ArrivalBatchItem(Base):
 __tablename__='arrival_batch_items'
 __table_args__=(UniqueConstraint('batch_id','row_no',name='uq_batch_row'),Index('ix_batch_items_status','status'),Index('ix_batch_items_identity','station','code','planned_day'),Index('ix_batch_items_replay_of','replay_of_item_id'))
 id=Column(Integer,primary_key=True)
 batch_id=Column(Integer,ForeignKey('arrival_batches.id'),nullable=False)
 row_no=Column(Integer,nullable=False)
 raw=Column(JSON,nullable=False)
 normalized=Column(JSON,nullable=True)
 status=Column(String(20),nullable=False,default='pending')
 reason=Column(Text,default='')
 arrival_id=Column(Integer,nullable=True)
 station=Column(String(30),nullable=True)
 code=Column(String(60),nullable=True)
 planned_day=Column(String(10),nullable=True)
 source_revision=Column(Integer,nullable=True)
 revoked_request=Column(Boolean,nullable=False,default=False)
 replay_of_item_id=Column(Integer,nullable=True)
 resolved_by_item_id=Column(Integer,nullable=True)
 resolution_status=Column(String(20),nullable=True)
 resolved_at=Column(String(40),nullable=True)
 processed_at=Column(String(40),nullable=True)
class ArrivalTombstone(Base):
 __tablename__='arrival_tombstones'
 __table_args__=(UniqueConstraint('station','code','planned_day',name='uq_tombstone_identity'),)
 id=Column(Integer,primary_key=True)
 station=Column(String(30),nullable=False)
 code=Column(String(60),nullable=False)
 planned_day=Column(String(10),nullable=False)
 revoked_revision=Column(Integer,nullable=False)
 revoked_at=Column(String(40),nullable=True)
 source=Column(String(80),nullable=True)
 content_hash=Column(String(64),nullable=True)
 last_batch_id=Column(Integer,nullable=True)
 last_item_id=Column(Integer,nullable=True)
