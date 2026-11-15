import random
from datetime import datetime,timedelta
from src.config.database import Base,engine,Session
from src.models.entities import User,Arrival,Subscription,Receipt
from src.services.ledger import content_hash
from src.utils.security import hash_password
from src.utils.time import now_utc,SH,next_due
from src.utils.time import aware

def seed():
 Base.metadata.create_all(engine);rng=random.Random(3113);now=now_utc();day=now.astimezone(SH).date()-timedelta(days=1)
 with Session() as s:
  for role in ('admin','analyst','viewer'):
   email=role+'@demo.local'
   if not s.query(User).filter_by(email=email).first():s.add(User(email=email,role=role,password=hash_password('Demo123!')))
  for i in range(150):
   code=f'LG-{i+1:03}';station=('北站','南站','东港')[i%3]
   planned=datetime.combine(day,datetime.min.time(),tzinfo=SH)+timedelta(minutes=i*8)
   delay=rng.choice([-5,0,0,3,5,8,12,18,25,40]);actual=planned+timedelta(minutes=delay)
   normalized={'code':code,'station':station,'planned_at':planned.astimezone(now.tzinfo).isoformat(),
    'planned_day':planned.date().isoformat(),'actual_at':None if i%29==0 else actual.astimezone(now.tzinfo).isoformat(),
    'note':'临港联运昨日交接班导出','revision':1,'revoked':False}
   existing=s.query(Arrival).filter_by(station=station,code=code,planned_day=normalized['planned_day']).first()
   if not existing:
    s.add(Arrival(code=code,station=station,planned_at=normalized['planned_at'],planned_day=normalized['planned_day'],
     actual_at=normalized['actual_at'],note=normalized['note'],source_revision=1,source_content=content_hash(normalized),
     revoked=False,source='seed',updated_at=now.isoformat()))
  for station in ('北站','南站','东港'):
   name=station+'值班晨报'
   if not s.query(Subscription).filter_by(name=name).first():s.add(Subscription(name=name,station=station,daily_at='08:30',recipient=station+'值班室',enabled=False,next_run_at=next_due('08:30',now).isoformat()))
  s.commit();print({'users':s.query(User).count(),'arrivals':s.query(Arrival).filter(Arrival.revoked.is_(False)).count(),'subscriptions':s.query(Subscription).count(),'receipts':s.query(Receipt).count()})
if __name__=='__main__':seed()
