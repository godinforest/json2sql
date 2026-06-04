from peewee import (
    SqliteDatabase, Model, BigIntegerField, CharField, 
    BooleanField, IntegerField, ForeignKeyField, AutoField
)

# Initialize database with None to allow dynamic connection in CLI
db = SqliteDatabase(None)

class BaseModel(Model):
    class Meta:
        database = db

class User(BaseModel):
    user_id = BigIntegerField(primary_key=True)
    username = CharField(max_length=32, null=True)
    first_name = CharField(max_length=100, default='Unknown')

class Chat(BaseModel):
    chat_id = BigIntegerField(primary_key=True)
    chat_type = CharField(max_length=20, default='unknown')

class UserProfile(BaseModel):
    id = AutoField()
    chat = ForeignKeyField(Chat, backref='profiles', on_delete='CASCADE')
    user = ForeignKeyField(User, backref='profiles', on_delete='CASCADE')
    language = CharField(max_length=2, default='en')

    class Meta:
        # Enforce unique combination of chat and user
        indexes = (
            (('chat', 'user'), True),
        )

class Pet(BaseModel):
    id = AutoField()
    # Link pet to the specific user profile
    profile = ForeignKeyField(UserProfile, backref='pets', on_delete='CASCADE')
    pet_id_local = IntegerField()
    pet_type = CharField(max_length=50)
    pet_name = CharField(max_length=100)
    feedings_per_day = IntegerField()
    portion_grams = IntegerField()
    interval_hours = IntegerField()
    first_feed_time_hhmm = CharField(max_length=5, null=True)

class FeedingRecord(BaseModel):
    id = AutoField()
    chat = ForeignKeyField(Chat, backref='feeding_records', on_delete='CASCADE')
    user = ForeignKeyField(User, backref='feeding_records', on_delete='SET NULL', null=True)
    day_iso = CharField(max_length=10)
    time_hhmm = CharField(max_length=5)
    fed = BooleanField()
    who = CharField(max_length=100)
    portion_grams = IntegerField()
    meal_name = CharField(max_length=50)

class NotificationState(BaseModel):
    id = AutoField()
    chat = ForeignKeyField(Chat, backref='notification_states', on_delete='CASCADE')
    day_iso = CharField(max_length=10)
    meal_index = IntegerField()
    is_sent = BooleanField()