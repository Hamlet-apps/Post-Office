"""Fill the database with the fictional Maple Grove Community Centre shown in the mockups.

    python manage.py seed_demo            # about 240 people
    python manage.py seed_demo --people 2418

Every name, address and number is made up. Tests and screenshots use this data;
no real organization's data appears anywhere in the repository.
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from campaigns.models import Campaign
from channels.models import EmailSender, PushApp, PushMember, TelegramBot, TelegramLink, Webhook
from commands.models import BotText, Command, CommandLog
from content.models import Content, Template
from core.models import ApiKey, Building, Language, Organization, TeamMember
from core.views import save_languages
from delivery.models import LinkClick, Message, TrackedLink
from people.models import ConsentRecord, List, Person, PersonExternalId, Segment, Subscription

DEMO_EMAIL = "andrew@maplegrove.example"
DEMO_PASSWORD = "maple-grove-demo"  # demo data only; change it after signing in

FIRST = ["Olena", "Taras", "Marie", "Andrew", "Priya", "Liam", "Sofia", "Noah", "Iryna", "Jean", "Mei", "Omar",
         "Chloe", "Bohdan", "Amara", "Lucas", "Nadia", "Ethan", "Yulia", "Hugo", "Grace", "Mykola", "Camille",
         "Daniel", "Oksana", "Felix", "Hannah", "Petro", "Elise", "Samir"]
LAST = ["Kovalenko", "Melnyk", "Tremblay", "Nguyen", "Sharma", "Walker", "Shevchenko", "Gagnon", "Chen", "Haddad",
        "Martin", "Bondarenko", "Okafor", "Roy", "Petrenko", "Lavoie", "Singh", "Brown", "Tkachenko", "Bouchard"]


def blocks(*items):
    return [dict(item, id=f"b{i + 1}") for i, item in enumerate(items)]


class Command(BaseCommand):  # noqa: F811 - Django requires this name
    help = "Create the fictional Maple Grove Community Centre demo data."

    def add_arguments(self, parser):
        parser.add_argument("--people", type=int, default=240)

    @transaction.atomic
    def handle(self, *args, **options):
        from commands.models import Command as BotCommand

        rnd = random.Random(20261120)
        now = timezone.now()

        org = Organization.get()
        org.name = "Maple Grove Community Centre"
        org.reply_to = "hello@maplegrove.example"
        org.mailing_address = "214 Alder Street, Maple Grove, BC V0V 0V0"
        org.website = "https://maplegrove.example"
        org.timezone = "America/Vancouver"
        org.setup_complete = True
        org.signup_form = {"show_language": True, "show_telegram": True, "show_first_name": True}
        org.save()
        save_languages(["en", "uk", "fr"], "en")
        langs = {l.code: l for l in Language.objects.all()}

        if not TeamMember.objects.filter(email=DEMO_EMAIL).exists():
            TeamMember.objects.create_user(DEMO_EMAIL, DEMO_PASSWORD, name="Andrew", role="owner")
        for email, name, role in [("olena@maplegrove.example", "Olena Kovalenko", "editor"),
                                  ("marie@maplegrove.example", "Marie Tremblay", "viewer")]:
            if not TeamMember.objects.filter(email=email).exists():
                TeamMember.objects.create_user(email, DEMO_PASSWORD, name=name, role=role)

        # ---- buildings
        def building(name, kind, url):
            b, _ = Building.objects.get_or_create(name=name, defaults={"kind": kind, "base_url": url})
            b.last_seen_at = now - timedelta(minutes=rnd.randint(2, 50))
            b.save()
            return b

        town_hall = building("Town Hall", "town_hall", "https://townhall.maplegrove.example")
        pavilion = building("Pavilion", "pavilion", "https://pavilion.maplegrove.example")
        bell_tower = building("Bell Tower", "bell_tower", "https://belltower.maplegrove.example")

        # ---- channels
        app_sender, _ = EmailSender.objects.get_or_create(purpose="app", defaults=dict(
            from_name="Maple Grove", from_email="notices@maplegrove.example", kind="smtp", host="smtp.maplegrove.example",
            port=587, username="notices@maplegrove.example", password="demo-not-a-real-password", state="ok", is_default=True))
        news_sender, _ = EmailSender.objects.get_or_create(from_email="news@maplegrove.example", defaults=dict(
            purpose="newsletter", from_name="Maple Grove News", kind="gmail", host="smtp.gmail.com", port=587,
            username="news@maplegrove.example", password="demo-not-a-real-password", state="ok", is_default=True,
            daily_limit=1500))
        bot, _ = TelegramBot.objects.get_or_create(username="MapleGroveBot", defaults=dict(
            name="Maple Grove", token="0000000000:demo-token-not-real", is_default=True))
        Webhook.objects.get_or_create(name="Lobby display", defaults=dict(url="https://lobby.maplegrove.example/hook"))

        # ---- lists
        def make_list(slug, kind, names, position, building=None, on_form=False, description=None):
            lst, _ = List.objects.get_or_create(slug=slug, defaults=dict(
                kind=kind, name=names, building=building, on_signup_form=on_form, position=position,
                description=description or {}))
            return lst

        newsletter = make_list("newsletter", "campaign", {"en": "Newsletter", "uk": "Розсилка", "fr": "Infolettre"}, 1,
                               on_form=True, description={"en": "Monthly news from the centre",
                                                          "uk": "Щомісячні новини центру", "fr": "Les nouvelles du mois"})
        events = make_list("events", "campaign", {"en": "Event updates", "uk": "Події", "fr": "Événements"}, 2,
                           on_form=True, description={"en": "Markets, suppers and workshops"})
        pav_news = make_list("pavilion-news", "campaign", {"en": "Pavilion news", "fr": "Nouvelles du Pavilion"}, 3,
                             building=pavilion)
        volunteers = make_list("volunteers", "app", {"en": "Volunteers", "uk": "Волонтери", "fr": "Bénévoles"}, 4, town_hall)
        customers = make_list("customers", "app", {"en": "Customers", "fr": "Clients"}, 5, pavilion)
        staff = make_list("staff", "app", {"en": "Staff"}, 6, pavilion)
        push_app, _ = PushApp.objects.get_or_create(building=pavilion, audience="offers-and-news", defaults=dict(
            label="Offers and news", endpoint="https://pavilion.maplegrove.example/api/post-office/push", list=pav_news))
        for name in ("Lapsed volunteers", "Opened the last three newsletters"):
            Segment.objects.get_or_create(name=name)

        # ---- people
        wording = "Yes, send me news from Maple Grove Community Centre. I can unsubscribe at any time."
        people = list(Person.objects.all())
        target = options["people"]
        made = 0
        while len(people) < target:
            n = len(people) + 1
            first, last = rnd.choice(FIRST), rnd.choice(LAST)
            code = rnd.choices(["en", "uk", "fr"], weights=[72, 18, 10])[0]
            joined = now - timedelta(days=rnd.randint(0, 400), hours=rnd.randint(0, 23))
            person = Person(email=f"{first}.{last}{n}@example.com".lower(), first_name=first, last_name=last,
                            language=langs[code], source=rnd.choice(["signup_form", "signup_form", "import", "Town Hall", "Pavilion"]),
                            email_state="bounced" if rnd.random() < 0.02 else "ok")
            person.save()
            Person.objects.filter(pk=person.pk).update(created_at=joined)
            person.created_at = joined
            people.append(person)
            made += 1

            consent = ConsentRecord.objects.create(person=person, method="signup_form" if person.source != "import" else "import",
                                                   wording=wording, language=code, ip="203.0.113.%d" % rnd.randint(1, 250),
                                                   user_agent="Mozilla/5.0 (demo)", given_at=joined, confirmed_at=joined)
            has_tg = rnd.random() < 0.3
            if has_tg:
                TelegramLink.objects.get_or_create(bot=bot, chat_id=48000000 + n, defaults=dict(
                    person=person, username=f"{first.lower()}{n}", blocked_at=now if rnd.random() < 0.03 else None))
            subs = []
            for lst, chance in ((newsletter, 0.85), (events, 0.5), (pav_news, 0.2)):
                if rnd.random() < chance:
                    consent.lists.add(lst)
                    state = "off" if rnd.random() < 0.04 else "on"
                    subs.append(Subscription(person=person, list=lst, channel="email", state=state, consent=consent))
                    if has_tg and rnd.random() < 0.7:
                        subs.append(Subscription(person=person, list=lst, channel="telegram", state="on", consent=consent))
            if rnd.random() < 0.16:
                PersonExternalId.objects.get_or_create(building=town_hall, external_id=f"vol_{n}", defaults={"person": person})
                subs.append(Subscription(person=person, list=volunteers, channel="email", state="on"))
                if has_tg:
                    subs.append(Subscription(person=person, list=volunteers, channel="telegram", state="on"))
            if rnd.random() < 0.22:
                PersonExternalId.objects.get_or_create(building=pavilion, external_id=f"cust_{n}", defaults={"person": person})
                subs.append(Subscription(person=person, list=customers, channel="email", state="on"))
                if rnd.random() < 0.4:
                    PushMember.objects.get_or_create(push_app=push_app, external_id=f"cust_{n}", defaults={"person": person})
                    subs.append(Subscription(person=person, list=pav_news, channel="push", state="on", consent=consent))
            Subscription.objects.bulk_create(subs, ignore_conflicts=True)

        # ---- app message templates
        def template(key, name, fires, building, variables, content):
            tpl, created = Template.objects.get_or_create(key=key, defaults=dict(
                kind="app", building=building, name=name, fires=fires, message_type="service",
                variables=variables, default_content=content))
            if created:
                for code, forms in content.items():
                    if "email" in forms:
                        Content.objects.create(template=tpl, language=code, form="email",
                                               subject=forms["email"]["subject"], blocks=forms["email"]["blocks"])
                    if "notification" in forms:
                        Content.objects.create(template=tpl, language=code, form="notification",
                                               text=forms["notification"]["text"])
            return tpl

        order_vars = {
            "customer.first_name": {"label": "first name", "sample": "Olena"},
            "order.number": {"label": "order number", "sample": "#0142"},
            "order.ready_time": {"label": "ready time", "sample": "6:40 PM"},
            "order.total": {"label": "order total", "sample": "$23.50"},
            "order.link": {"label": "order link", "sample": "https://pavilion.maplegrove.example/orders/0142"},
            "order.items": {"label": "order items", "list": {"name": {}, "quantity": {}, "price": {}, "notes": {}},
                            "sample": [{"name": "Borscht", "quantity": 2, "price": "$9.00", "notes": ""},
                                       {"name": "Varenyky", "quantity": 1, "price": "$5.50", "notes": "extra sour cream"}]},
        }
        order_tpl = template("pavilion.order_confirmation", "Order confirmation", "When an order is placed", pavilion, order_vars, {
            "en": {"email": {"subject": "Your order {order.number} is confirmed", "blocks": blocks(
                {"type": "heading", "text": "Thanks, {customer.first_name}"},
                {"type": "text", "text": "We have your order {order.number}. It will be ready at *{order.ready_time}*."},
                {"type": "repeat", "source": "order.items", "show": ["name", "quantity", "price"]},
                {"type": "card", "title": "Total", "text": "{order.total}"},
                {"type": "button", "text": "View your order", "href": "{order.link}"})},
                "notification": {"text": "Order {order.number} is confirmed. Ready at *{order.ready_time}*."}},
            "uk": {"email": {"subject": "Ваше замовлення {order.number} підтверджено", "blocks": blocks(
                {"type": "heading", "text": "Дякуємо, {customer.first_name}"},
                {"type": "text", "text": "Ми отримали замовлення {order.number}. Воно буде готове о *{order.ready_time}*."},
                {"type": "repeat", "source": "order.items", "show": ["name", "quantity", "price"]},
                {"type": "button", "text": "Переглянути замовлення", "href": "{order.link}"})},
                "notification": {"text": "Замовлення {order.number} підтверджено. Буде готове о *{order.ready_time}*."}},
        })
        ready_tpl = template("pavilion.order_ready", "Order ready", "When the kitchen marks an order ready", pavilion,
                             {"customer.first_name": {"label": "first name", "sample": "Olena"},
                              "order.number": {"label": "order number", "sample": "#0142"}},
                             {"en": {"notification": {"text": "{customer.first_name}, your order {order.number} is ready to pick up."}}})
        shift_tpl = template("townhall.shift_reminder", "Shift reminder", "The day before a shift", town_hall, {
            "volunteer.first_name": {"label": "first name", "sample": "Taras"},
            "shift.role": {"label": "role", "sample": "Greeter"},
            "shift.start": {"label": "start time", "sample": "Saturday 9:00 AM"},
            "shift.place": {"label": "place", "sample": "Main hall"},
            "shift.link": {"label": "shift link", "sample": "https://townhall.maplegrove.example/shifts/88"},
        }, {
            "en": {"email": {"subject": "Your shift tomorrow: {shift.role}", "blocks": blocks(
                {"type": "heading", "text": "See you tomorrow, {volunteer.first_name}"},
                {"type": "card", "title": "{shift.role}", "text": "{shift.start}\n{shift.place}"},
                {"type": "button", "text": "Open your shift", "href": "{shift.link}"})},
                "notification": {"text": "Reminder: *{shift.role}*, {shift.start}, {shift.place}."}},
            "uk": {"email": {"subject": "Ваша зміна завтра: {shift.role}", "blocks": blocks(
                {"type": "heading", "text": "До завтра, {volunteer.first_name}"},
                {"type": "card", "title": "{shift.role}", "text": "{shift.start}\n{shift.place}"},
                {"type": "button", "text": "Відкрити зміну", "href": "{shift.link}"})},
                "notification": {"text": "Нагадування: *{shift.role}*, {shift.start}, {shift.place}."}},
            "fr": {"email": {"subject": "Votre quart demain : {shift.role}", "blocks": blocks(
                {"type": "heading", "text": "À demain, {volunteer.first_name}"},
                {"type": "card", "title": "{shift.role}", "text": "{shift.start}\n{shift.place}"},
                {"type": "button", "text": "Voir votre quart", "href": "{shift.link}"})},
                "notification": {"text": "Rappel : *{shift.role}*, {shift.start}, {shift.place}."}},
        })
        digest_tpl = template("belltower.task_digest", "Task digest", "Every weekday morning", bell_tower,
                              {"person.first_name": {"label": "first name", "sample": "Marie"},
                               "tasks.count": {"label": "open tasks", "sample": "4"}},
                              {"en": {"email": {"subject": "You have {tasks.count} open tasks", "blocks": blocks(
                                  {"type": "text", "text": "Good morning. You have {tasks.count} open tasks today."})}}})

        # ---- a campaign template (starting point)
        starter, created = Template.objects.get_or_create(key="campaign.monthly-news", defaults=dict(
            kind="campaign", name="Monthly news", message_type="newsletter", fires="A heading, a story, an event card and a button"))
        if created:
            Content.objects.create(template=starter, language="en", form="email", subject="This month at Maple Grove", blocks=blocks(
                {"type": "heading", "text": "This month at Maple Grove"},
                {"type": "text", "text": "Hi {first_name},\nHere is what is coming up."},
                {"type": "card", "title": "Event name", "text": "Day, time and place"},
                {"type": "button", "text": "See the full calendar", "href": "https://maplegrove.example/calendar"}))

        # ---- campaigns
        def campaign(name, state, days_ago, written=("en", "uk", "fr"), channels=("email", "telegram"), scheduled_in=None):
            c, created = Campaign.objects.get_or_create(name=name, defaults=dict(
                state=state, channels=list(channels), sender=news_sender, bot=bot,
                created_by=TeamMember.objects.filter(email=DEMO_EMAIL).first()))
            if not created:
                return c, False
            c.lists.add(newsletter)
            when = now - timedelta(days=days_ago)
            if state == "sent":
                c.started_at = when
                c.sent_at = when + timedelta(minutes=12)
            if scheduled_in is not None:
                c.scheduled_at = now + timedelta(days=scheduled_in)
            c.save()
            Campaign.objects.filter(pk=c.pk).update(created_at=when - timedelta(days=2))
            text = {
                "en": ("Hi {first_name},\nDecember is a busy month at the centre.", "See the full calendar", name),
                "uk": ("Вітаємо, {first_name},\nГрудень у центрі буде насиченим.", "Переглянути календар", name),
                "fr": ("Bonjour {first_name},\nDécembre sera bien rempli au centre.", "Voir le calendrier", name),
            }
            for code in written:
                body, button, subject = text[code]
                Content.objects.create(campaign=c, language=code, form="email", subject=subject, preview_text="News from Maple Grove", blocks=blocks(
                    {"type": "heading", "text": subject, "align": "left"},
                    {"type": "text", "text": body},
                    {"type": "card", "title": "Holiday market", "text": "Saturday, December 12, 10 AM to 3 PM, main hall"},
                    {"type": "button", "text": button, "href": "https://maplegrove.example/calendar"},
                    {"type": "divider"}))
                Content.objects.create(campaign=c, language=code, form="notification",
                                       text=f"*{subject}*\n" + body.split("\n", 1)[1],
                                       button_label=button, button_url="https://maplegrove.example/calendar")
            return c, True

        sent_campaigns = []
        for name, days in (("Fall supper reminder", 26), ("Volunteer thank-you", 16), ("Winter coat drive", 5)):
            c, created = campaign(name, "sent", days)
            if created:
                sent_campaigns.append((c, days))
        campaign("December at Maple Grove", "scheduled", 0, scheduled_in=3)
        campaign("Holiday market preview", "draft", 0, written=("en", "uk"))

        # ---- messages for the sent campaigns, with opens and clicks
        subscribers = list(Person.objects.filter(subscriptions__list=newsletter, subscriptions__state="on").distinct())
        tg_ids = set(TelegramLink.objects.filter(bot=bot, blocked_at__isnull=True).values_list("person_id", flat=True))
        tg_subs = set(Subscription.objects.filter(list=newsletter, channel="telegram", state="on").values_list("person_id", flat=True))
        for c, days in sent_campaigns:
            link = TrackedLink.objects.create(campaign=c, url="https://maplegrove.example/calendar")
            sent_at = now - timedelta(days=days)
            rows = []
            for p in subscribers:
                code = p.language_code
                if p.email_reachable:
                    roll = rnd.random()
                    opened = sent_at + timedelta(hours=rnd.expovariate(1 / 9)) if roll < 0.5 else None
                    clicked = opened + timedelta(minutes=rnd.randint(1, 40)) if opened and rnd.random() < 0.13 else None
                    failed = roll > 0.985
                    rows.append(Message(
                        person=p, source="campaign", message_type="newsletter", campaign=c, list=newsletter, channel="email",
                        language=code, sender=news_sender, subject_snapshot=c.name,
                        state="failed" if failed else "clicked" if clicked else "opened" if opened else "sent",
                        error="The mail server said this mailbox is full" if failed else "",
                        idempotency_key=f"{c.public_id}:{p.public_id}:email", sent_at=None if failed else sent_at,
                        opened_at=opened, clicked_at=clicked, attempts=1))
                if p.pk in tg_ids and p.pk in tg_subs:
                    rows.append(Message(
                        person=p, source="campaign", message_type="newsletter", campaign=c, list=newsletter, channel="telegram",
                        language=code, bot=bot, subject_snapshot=c.name, state="sent",
                        idempotency_key=f"{c.public_id}:{p.public_id}:telegram", sent_at=sent_at, attempts=1))
            Message.objects.bulk_create(rows, batch_size=1000)
            Message.objects.filter(campaign=c).update(created_at=sent_at)
            clicks = [LinkClick(message=m, link=link) for m in Message.objects.filter(campaign=c, clicked_at__isnull=False)]
            LinkClick.objects.bulk_create(clicks)
            for click in LinkClick.objects.filter(link=link).select_related("message"):
                LinkClick.objects.filter(pk=click.pk).update(at=click.message.clicked_at)

        # ---- app messages over the last two weeks
        if not Message.objects.filter(source="building").exists():
            rows, n = [], 0
            vols = list(Person.objects.filter(subscriptions__list=volunteers).distinct())
            custs = list(Person.objects.filter(subscriptions__list=customers).distinct())
            plan = [(shift_tpl, town_hall, vols, 14), (order_tpl, pavilion, custs, 9), (ready_tpl, pavilion, custs, 7),
                    (digest_tpl, bell_tower, vols[:20], 3)]
            for tpl, bld, pool, per_day in plan:
                if not pool:
                    continue
                for day in range(14):
                    for _ in range(per_day if day else per_day * 2):
                        p = rnd.choice(pool)
                        at = now - timedelta(days=day, minutes=rnd.randint(0, 600))
                        if at > now:
                            at = now - timedelta(minutes=rnd.randint(2, 50))
                        channel = "telegram" if (p.pk in tg_ids and tpl.contents.filter(form="notification").exists() and rnd.random() < 0.4) else "email"
                        if channel == "email" and not tpl.contents.filter(form="email").exists():
                            continue
                        n += 1
                        failed = rnd.random() < 0.012
                        rows.append(Message(
                            person=p, source="building", message_type="service", template=tpl, building=bld, channel=channel,
                            language=p.language_code, used_fallback=p.language_code not in ("en", "uk"),
                            sender=app_sender if channel == "email" else None, bot=bot if channel == "telegram" else None,
                            state="failed" if failed else "sent",
                            error="Telegram says this person has blocked the bot" if failed and channel == "telegram"
                            else "The mail server refused this address" if failed else "",
                            subject_snapshot=tpl.name, idempotency_key=f"demo-{n}:{p.public_id}:{channel}",
                            sent_at=None if failed else at, attempts=1, created_at=at))
            Message.objects.bulk_create(rows, batch_size=1000)
            for m in rows:
                Message.objects.filter(idempotency_key=m.idempotency_key).update(created_at=m.sent_at or now - timedelta(hours=3))

        # ---- Telegram commands
        BotText.objects.get_or_create(bot=bot, language="en", defaults=dict(
            unknown_command="Sorry, I do not know that command. Send /help to see what I can do.",
            no_answer="Sorry, that did not work just now. Please try again in a few minutes."))
        BotText.objects.get_or_create(bot=bot, language="uk", defaults=dict(
            unknown_command="Вибачте, я не знаю такої команди. Надішліть /help, щоб побачити, що я вмію.",
            no_answer="Вибачте, зараз не вдалося. Спробуйте ще раз за кілька хвилин."))
        shifts, _ = BotCommand.objects.get_or_create(bot=bot, name="shifts", defaults=dict(
            descriptions={"en": "Your upcoming shifts", "uk": "Ваші найближчі зміни", "fr": "Vos prochains quarts"},
            target="http", method="POST", url="https://townhall.maplegrove.example/api/bot/shifts",
            body='{"command": "{{command}}", "arguments": "{{arguments}}", "person": "{{person.id}}"}',
            reply_path="reply"))
        BotCommand.objects.get_or_create(bot=bot, name="menu", defaults=dict(
            descriptions={"en": "Today's menu at the Pavilion", "fr": "Le menu du jour au Pavilion"},
            target="http", method="GET", url="https://pavilion.maplegrove.example/api/bot/menu?lang={{person.language}}",
            reply_path="text", connected_only=False))
        if not CommandLog.objects.exists():
            for i in range(12):
                p = rnd.choice(people)
                CommandLog.objects.create(bot=bot, person=p, chat_id=48000000 + p.pk, text=rnd.choice(["/shifts", "/shifts saturday", "/menu", "/help", "/hours"]),
                                          command=rnd.choice(["shifts", "menu", "help"]), target="http",
                                          result=rnd.choices(["answered", "no_answer", "unknown"], weights=[8, 1, 1])[0],
                                          duration_ms=rnd.randint(120, 2400))

        if not ApiKey.objects.exists():
            ApiKey.issue("Website signup form", "subscribe")

        self.stdout.write(self.style.SUCCESS(
            f"Maple Grove Community Centre is ready: {Person.objects.count()} people ({made} new), "
            f"{Campaign.objects.count()} campaigns, {Message.objects.count()} messages.\n"
            f"Sign in as {DEMO_EMAIL} with the demo password set in core/management/commands/seed_demo.py."
        ))
