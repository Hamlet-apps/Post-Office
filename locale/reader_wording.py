"""Ukrainian and French for the wording readers see.

This covers the built-in wording the spec says must ship translated: email
footers, the signup form, the confirmation email, the preferences page and the
bot's own replies. The staff screens are not translated yet.

To apply after `manage.py makemessages -l uk -l fr --no-location`:

    pip install polib
    python locale/reader_wording.py
    python manage.py compilemessages --ignore=.venv
"""
from pathlib import Path

WORDING = {
    # msgid: (Ukrainian, French)
    # --- email footers
    "Unsubscribe": ("Відписатися", "Se désabonner"),
    "Manage preferences": ("Керувати налаштуваннями", "Gérer mes préférences"),
    "Get this on Telegram": ("Отримувати це в Telegram", "Recevoir ceci sur Telegram"),
    "You are getting this because you signed up for %(list)s.":
        ("Ви отримали цей лист, бо підписалися на «%(list)s».", "Vous recevez ce message parce que vous êtes inscrit à « %(list)s »."),
    "You are getting this because you signed up for news from us.":
        ("Ви отримали цей лист, бо підписалися на наші новини.", "Vous recevez ce message parce que vous êtes inscrit à nos nouvelles."),
    "You are getting this because of something you did with us, such as an order or a shift.":
        ("Ви отримали цей лист через вашу дію в нас, наприклад замовлення чи зміну.",
         "Vous recevez ce message à la suite d’une action de votre part, comme une commande ou un quart."),
    # --- signup form
    "Stay in the loop": ("Будьте в курсі", "Restez informé"),
    "Subscribe": ("Підписатися", "S’abonner"),
    "News and events from %(org)s, in your language.":
        ("Новини та події від %(org)s вашою мовою.", "Les nouvelles et événements de %(org)s, dans votre langue."),
    "First name": ("Ім’я", "Prénom"),
    "Email": ("Електронна пошта", "Courriel"),
    "What would you like to hear about?": ("Про що ви хочете отримувати повідомлення?", "De quoi souhaitez-vous entendre parler ?"),
    "Also send me updates on Telegram": ("Також надсилайте мені повідомлення в Telegram", "Envoyez-moi aussi les nouvelles sur Telegram"),
    "I agree to receive these messages. I can unsubscribe at any time.":
        ("Я погоджуюся отримувати ці повідомлення. Я можу відписатися будь-коли.",
         "J’accepte de recevoir ces messages. Je peux me désabonner en tout temps."),
    "By pressing Subscribe, I agree to receive these messages. I can unsubscribe at any time.":
        ("Натискаючи «Підписатися», я погоджуюся отримувати ці повідомлення. Я можу відписатися будь-коли.",
         "En appuyant sur S’abonner, j’accepte de recevoir ces messages. Je peux me désabonner en tout temps."),
    "Signing up is not open right now. Please come back later.":
        ("Підписка зараз недоступна. Будь ласка, зайдіть пізніше.", "L’inscription n’est pas ouverte pour le moment. Revenez plus tard."),
    "Enter your email address, like name@example.com.":
        ("Введіть вашу електронну адресу, наприклад name@example.com.", "Entrez votre adresse courriel, par exemple nom@example.com."),
    "Tick at least one thing you would like to hear about.":
        ("Позначте хоча б одну тему, яка вас цікавить.", "Cochez au moins un sujet qui vous intéresse."),
    "Tick the box to say you agree to receive these messages.":
        ("Поставте позначку, щоб підтвердити згоду отримувати ці повідомлення.",
         "Cochez la case pour confirmer que vous acceptez de recevoir ces messages."),
    "Too many signups came from your connection. Please try again in an hour.":
        ("З вашого з’єднання надійшло забагато заявок. Спробуйте ще раз за годину.",
         "Trop d’inscriptions proviennent de votre connexion. Réessayez dans une heure."),
    # --- thank-you and confirmation pages
    "Thank you, %(name)s": ("Дякуємо, %(name)s", "Merci, %(name)s"),
    "Thank you": ("Дякуємо", "Merci"),
    "Check your inbox. We sent you an email with one last step.":
        ("Перевірте пошту. Ми надіслали вам лист з останнім кроком.", "Vérifiez votre boîte de réception. Nous vous avons envoyé un courriel avec une dernière étape."),
    "Nothing after a few minutes? Look in your spam folder, or fill in the form again.":
        ("Нічого не прийшло за кілька хвилин? Перевірте папку «Спам» або заповніть форму ще раз.",
         "Rien après quelques minutes ? Regardez dans vos pourriels ou remplissez le formulaire de nouveau."),
    "You are signed up.": ("Ви підписані.", "Vous êtes inscrit."),
    "If you were already with us, we sent you an email with a link to your choices instead.":
        ("Якщо ви вже були з нами, ми натомість надіслали вам лист із посиланням на ваші налаштування.",
         "Si vous étiez déjà inscrit, nous vous avons plutôt envoyé un courriel avec un lien vers vos choix."),
    "One more step for Telegram": ("Ще один крок для Telegram", "Encore une étape pour Telegram"),
    "Open our bot in Telegram and press Start. That connects it to you.":
        ("Відкрийте нашого бота в Telegram і натисніть Start. Так він під’єднається до вас.",
         "Ouvrez notre robot dans Telegram et appuyez sur Start. Il sera alors relié à vous."),
    "Open Telegram": ("Відкрити Telegram", "Ouvrir Telegram"),
    "You are confirmed": ("Підтверджено", "C’est confirmé"),
    "Your email address is confirmed.": ("Вашу електронну адресу підтверджено.", "Votre adresse courriel est confirmée."),
    "You will hear from us about:": ("Ви отримуватимете від нас:", "Vous recevrez de nos nouvelles sur :"),
    "This link no longer works": ("Це посилання більше не працює", "Ce lien ne fonctionne plus"),
    "It may be too old, or part of it went missing when it was copied. Use the link in a newer message from us, or sign up again.":
        ("Можливо, воно застаріле або скопіювалося не повністю. Скористайтеся посиланням із новішого повідомлення від нас або підпишіться знову.",
         "Il est peut-être trop ancien, ou une partie a été perdue en le copiant. Utilisez le lien d’un message plus récent ou inscrivez-vous de nouveau."),
    "Go to the signup form": ("Перейти до форми підписки", "Aller au formulaire d’inscription"),
    # --- confirmation and preferences emails
    "Please confirm your email address": ("Підтвердьте, будь ласка, вашу електронну адресу", "Veuillez confirmer votre adresse courriel"),
    "Hi %(name)s,": ("Вітаємо, %(name)s,", "Bonjour %(name)s,"),
    "Hi,": ("Вітаємо,", "Bonjour,"),
    "You asked to hear from %(org)s about: %(lists)s. Press the button to confirm this is your email address.":
        ("Ви попросили отримувати від %(org)s: %(lists)s. Натисніть кнопку, щоб підтвердити, що це ваша електронна адреса.",
         "Vous avez demandé à recevoir de %(org)s : %(lists)s. Appuyez sur le bouton pour confirmer qu’il s’agit bien de votre adresse courriel."),
    "If this was not you, ignore this email and you will hear nothing more.":
        ("Якщо це були не ви, проігноруйте цей лист, і більше нічого не надійде.",
         "Si ce n’était pas vous, ignorez ce courriel et vous ne recevrez rien d’autre."),
    "Confirm my email address": ("Підтвердити мою адресу", "Confirmer mon adresse courriel"),
    "Your choices at %(org)s": ("Ваші налаштування в %(org)s", "Vos choix chez %(org)s"),
    "Someone, probably you, just filled in our signup form with this email address. You are already with us, so nothing was changed. Press the button to see and change what you hear about.":
        ("Хтось, імовірно ви, щойно заповнив нашу форму підписки з цією адресою. Ви вже з нами, тож нічого не змінилося. Натисніть кнопку, щоб переглянути й змінити, що ви отримуєте.",
         "Quelqu’un, probablement vous, vient de remplir notre formulaire d’inscription avec cette adresse. Vous êtes déjà inscrit, donc rien n’a changé. Appuyez sur le bouton pour voir et modifier ce que vous recevez."),
    "If this was not you, there is nothing you need to do.":
        ("Якщо це були не ви, нічого робити не потрібно.", "Si ce n’était pas vous, vous n’avez rien à faire."),
    "Choose what I hear about": ("Обрати, що я отримую", "Choisir ce que je reçois"),
    # --- preferences page
    "Choose what you hear about": ("Оберіть, що ви отримуєте", "Choisissez ce que vous recevez"),
    "%(name)s, choose what you hear about": ("%(name)s, оберіть, що ви отримуєте", "%(name)s, choisissez ce que vous recevez"),
    "Tick how you want to get each kind of message. Changes save as you go.":
        ("Позначте, як ви хочете отримувати кожен вид повідомлень. Зміни зберігаються одразу.",
         "Cochez la façon dont vous voulez recevoir chaque type de message. Les changements s’enregistrent au fur et à mesure."),
    "News": ("Новини", "Nouvelles"),
    "About things you signed up for": ("Про те, на що ви записалися", "À propos de ce à quoi vous vous êtes inscrit"),
    "You get each message everywhere you ticked. Notifications inside our apps about your own orders and shifts are switched on and off in those apps.":
        ("Ви отримуєте кожне повідомлення всюди, де стоїть позначка. Сповіщення в наших застосунках про ваші замовлення та зміни вмикаються й вимикаються в самих застосунках.",
         "Vous recevez chaque message partout où vous avez coché. Les notifications de nos applications sur vos commandes et vos quarts se règlent dans ces applications."),
    "Unsubscribe from all news": ("Відписатися від усіх новин", "Se désabonner de toutes les nouvelles"),
    "Done": ("Готово", "Terminé"),
    "You are unsubscribed from all news. You can tick anything back on whenever you like.":
        ("Ви відписалися від усіх новин. Ви можете будь-коли знову поставити позначки.",
         "Vous êtes désabonné de toutes les nouvelles. Vous pouvez recocher ce que vous voulez à tout moment."),
    "You will no longer get %(lists)s by email. Anything else you get is listed below.":
        ("Ви більше не отримуватимете «%(lists)s» електронною поштою. Усе інше, що ви отримуєте, показано нижче.",
         "Vous ne recevrez plus « %(lists)s » par courriel. Tout ce que vous recevez encore figure ci-dessous."),
    "Telegram is not connected yet, so nothing can arrive there. Open our bot and press Start.":
        ("Telegram ще не під’єднано, тому туди нічого не надійде. Відкрийте нашого бота й натисніть Start.",
         "Telegram n’est pas encore relié, donc rien ne peut y arriver. Ouvrez notre robot et appuyez sur Start."),
    "Connect Telegram": ("Під’єднати Telegram", "Relier Telegram"),
    "Your choices are saved": ("Ваші налаштування збережено", "Vos choix sont enregistrés"),
    "You can close this page. The link at the bottom of any message from us brings you back.":
        ("Можете закрити цю сторінку. Посилання внизу будь-якого нашого повідомлення поверне вас сюди.",
         "Vous pouvez fermer cette page. Le lien au bas de chacun de nos messages vous y ramènera."),
    "Change something else": ("Змінити ще щось", "Modifier autre chose"),
    "waiting for you to confirm": ("очікує вашого підтвердження", "en attente de votre confirmation"),
    "Waiting for you to confirm. Press to switch it on now.":
        ("Очікує вашого підтвердження. Натисніть, щоб увімкнути зараз.", "En attente de votre confirmation. Appuyez pour l’activer maintenant."),
    "not available": ("недоступно", "non disponible"),
    "%(list)s by email": ("«%(list)s» електронною поштою", "%(list)s par courriel"),
    "%(list)s by Telegram": ("«%(list)s» у Telegram", "%(list)s par Telegram"),
    "%(list)s by push": ("«%(list)s» push-сповіщенням", "%(list)s par notification"),
    "Sent with Post Office": ("Надіслано через Post Office", "Envoyé avec Post Office"),
    "Push": ("Push", "Notifications"),
    # --- the bot's own replies
    "Connect this chat to your subscription": ("Під’єднати цей чат до вашої підписки", "Relier cette conversation à votre abonnement"),
    "Stop newsletters here": ("Зупинити розсилки тут", "Arrêter les infolettres ici"),
    "Change your language": ("Змінити мову", "Changer de langue"),
    "See what I can do": ("Подивитися, що я вмію", "Voir ce que je sais faire"),
    "Sorry, I do not know that one. Type /help to see what I can do.":
        ("Вибачте, я не знаю такої команди. Надішліть /help, щоб побачити, що я вмію.",
         "Désolé, je ne connais pas cette commande. Tapez /help pour voir ce que je sais faire."),
    "Sorry, that is not working right now. Please try again later.":
        ("Вибачте, зараз це не працює. Спробуйте, будь ласка, пізніше.", "Désolé, cela ne fonctionne pas pour le moment. Réessayez plus tard."),
    "This link has already been used or is too old. Open your preferences page to get a new one, or sign up here: %(url)s":
        ("Це посилання вже використано або воно застаріле. Відкрийте сторінку налаштувань, щоб отримати нове, або підпишіться тут: %(url)s",
         "Ce lien a déjà été utilisé ou est trop ancien. Ouvrez votre page de préférences pour en obtenir un nouveau, ou inscrivez-vous ici : %(url)s"),
    "You are connected, %(name)s.": ("Вас під’єднано, %(name)s.", "Vous êtes relié, %(name)s."),
    "You are connected.": ("Вас під’єднано.", "Vous êtes relié."),
    "Messages you signed up for will arrive here. Choose what you get: %(url)s":
        ("Повідомлення, на які ви підписалися, надходитимуть сюди. Оберіть, що отримувати: %(url)s",
         "Les messages auxquels vous êtes inscrit arriveront ici. Choisissez ce que vous recevez : %(url)s"),
    "You are already connected. Choose what you get here: %(url)s":
        ("Вас уже під’єднано. Оберіть, що отримувати, тут: %(url)s", "Vous êtes déjà relié. Choisissez ce que vous recevez ici : %(url)s"),
    "Welcome. To get messages here, sign up first: %(url)s":
        ("Вітаємо. Щоб отримувати повідомлення тут, спершу підпишіться: %(url)s",
         "Bienvenue. Pour recevoir des messages ici, inscrivez-vous d’abord : %(url)s"),
    "This chat is not connected, so there is nothing to stop.":
        ("Цей чат не під’єднано, тож зупиняти нічого.", "Cette conversation n’est pas reliée, il n’y a donc rien à arrêter."),
    "Done. You will not get newsletters here any more. Messages about things you signed up for still arrive. Change your mind any time: %(url)s":
        ("Готово. Розсилки сюди більше не надходитимуть. Повідомлення про те, на що ви записалися, надходитимуть і далі. Передумати можна будь-коли: %(url)s",
         "C’est fait. Vous ne recevrez plus d’infolettres ici. Les messages liés à vos inscriptions continueront d’arriver. Changez d’avis à tout moment : %(url)s"),
    "Which language would you like?": ("Якою мовою вам писати?", "Quelle langue préférez-vous ?"),
    "Here is what I can do:": ("Ось що я вмію:", "Voici ce que je sais faire :"),
    "That is a lot of commands. Please wait a minute and try again.":
        ("Забагато команд. Зачекайте хвилину й спробуйте знову.", "Cela fait beaucoup de commandes. Attendez une minute et réessayez."),
    "This one is for people who are connected. Press /start to connect, then try again.":
        ("Ця команда для під’єднаних людей. Натисніть /start, щоб під’єднатися, і спробуйте знову.",
         "Cette commande est réservée aux personnes reliées. Appuyez sur /start pour vous relier, puis réessayez."),
    "Saved. I will write to you in %(language)s.": ("Збережено. Я писатиму вам такою мовою: %(language)s.", "C’est noté. Je vous écrirai en %(language)s."),
}


def main():
    import polib

    root = Path(__file__).resolve().parent
    for index, code in enumerate(("uk", "fr")):
        path = root / code / "LC_MESSAGES" / "django.po"
        po = polib.pofile(str(path))
        ids = {e.msgid: e for e in po}
        done, missing = 0, []
        for msgid, pair in WORDING.items():
            entry = ids.get(msgid)
            if entry is None:
                missing.append(msgid)
                continue
            entry.msgstr = pair[index]
            if "fuzzy" in entry.flags:
                entry.flags.remove("fuzzy")
            done += 1
        po.save()
        print(f"{code}: {done} translated, {len(missing)} not found in the catalogue")
        for msgid in missing:
            print("   missing:", msgid[:70])


if __name__ == "__main__":
    main()
