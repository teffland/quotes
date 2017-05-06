import time
import sched
import datetime
import pymongo
import numpy as np

import smtplib
import imaplib
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.message import MIMEMessage

class Server(object):
    def __init__(self, db_config, email_config):
        self.email_config = email_config
        self.EMAIL_ADDRESS = email_config['FROM_UID'] + email_config['EMAIL_ORG']
        self.FROM_PWD = email_config['FROM_PWD']
        SMTP_SERVER = email_config['SMTP_SERVER']
        SMTP_PORT = email_config['SMTP_PORT']
        IMAP_SERVER = email_config['IMAP_SERVER']

        # set up to send emails
        self.sendserver = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        self.sendserver.ehlo()
        self.sendserver.starttls()
        self.sendserver.ehlo()
        self.sendserver.login(self.EMAIL_ADDRESS, self.FROM_PWD)

        # set up to receive emails
        self.inbox = imaplib.IMAP4_SSL(IMAP_SERVER)
        self.inbox.login(self.EMAIL_ADDRESS, self.FROM_PWD)
        self.inbox.select('inbox')

        #
        self.OPEN_QUOTES = [ '"', "'", "\xe2\x80\x9c" ]
        self.CLOSE_QUOTES = [ '"', "'", "\xe2\x80\x9d"]

        # load storage
        self.db = pymongo.MongoClient().db

    def get_new_emails(self):
        try:
            # query for the email ids
            search_status, search_data = self.inbox.search(None, 'ALL')

            # get emails we already know about
            seen_email_ids = { datum['inbox_id'] for datum in self.db.emails.find() }

            # unique ids for each
            mail_ids = search_data[0].split()
            if len(mail_ids) == 0:
                print "Inbox is empty"
                return
            first_email_id = int(mail_ids[0])
            latest_email_id = int(mail_ids[-1])

            # iterate over inbox, newest first
            for email_id in range(latest_email_id, first_email_id-1, -1):
                if email_id in seen_email_ids: continue
                email_status, email_data = self.inbox.fetch(email_id, '(RFC822)' )

                for response_part in email_data:
                    # it's an email, parse it
                    if isinstance(response_part, tuple):
                        msg = email.message_from_string(response_part[1])
                        email_subject = msg['subject']
                        email_from = msg['from']
                        content = ''
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True)
                                for c in self.OPEN_QUOTES + self.CLOSE_QUOTES:
                                    body = body.replace(c, '"')
                                content += body
                        msg['content'] = content
                        yield email_id, msg


        except Exception, e:
            print str(e)

    def send_email(self, to, subject, body_text):
        new = MIMEMultipart("mixed")
        body = MIMEMultipart("alternative")
        body.attach( MIMEText(body_text.encode('utf-8'), "plain") )
        # body.attach( MIMEText("<html>reply body text</html>", "html") )
        new.attach(body)

        new["Message-ID"] = email.utils.make_msgid()
        new["Subject"] = subject
        new["To"] = to
        new["From"] = self.EMAIL_ADDRESS

        self.sendserver.sendmail(self.EMAIL_ADDRESS, [new["To"]], new.as_string())

    def reply_to_email(self, og_email, body_text="Got it!"):
        # http://stackoverflow.com/questions/64505/sending-mail-from-python-using-smtp
        new = MIMEMultipart("mixed")
        body = MIMEMultipart("alternative")
        body.attach( MIMEText(body_text, "plain") )
        # body.attach( MIMEText("<html>reply body text</html>", "html") )
        new.attach(body)

        new["Message-ID"] = email.utils.make_msgid()
        new["In-Reply-To"] = og_email["Message-ID"]
        new["References"] = og_email["Message-ID"]
        new["Subject"] = "Re: "+og_email["Subject"]
        new["To"] = og_email["Reply-To"] or og_email["From"]
        new["From"] = self.EMAIL_ADDRESS

        new.attach( MIMEMessage(og_email) )
        # print "replying...",
        self.sendserver.sendmail(self.EMAIL_ADDRESS, [new["To"]], new.as_string())
        # print "success"

    def check_mail(self, *_):
        # search the inbox
        print "Checking mail"
        for email_id, inmail in self.get_new_emails():
            print "GOT MAIL (id:{})".format(email_id)
            email_dict = {
                'inbox_id':email_id,
                'from':inmail['from'],
                'content':inmail['content'],
                'timestamp':datetime.datetime.now(),
                'acknowledged': False
            }
            result = self.db.emails.insert_one(email_dict)
            if result.acknowledged:
                db_id = result.inserted_id
            else:
                print " [WARNING] failed to insert email into db"
                continue

            # is this user new? change reply content based on this
            quote_count = self.db.quotes.find({'user':inmail['from']}).count()
            if quote_count:
                print 'Known user: {}'.format(inmail['from'])
                reply_message  = """Added this quote to your list!

You now have {} quotes so far. Nice!
                """.format(quote_count)
            else:
                print 'New user: {}'.format(inmail['from'])
                reply_message = """Welcome to the Personal Quote Service!

We've added your quote to your list.

Check out more at blah.com
                """

            # persist the quote
            new_quote = {
                'user':inmail['from'],
                'content': inmail['content'],
                'email_id': email_id,
                'timestamp': datetime.datetime.now()
            }
            result = self.db.quotes.insert_one(new_quote)
            if not result.acknowledged:
                print " [WARNING] Failed to persist quote to db"
                continue

            # send acknowledgement to the user
            try:
                self.reply_to_email(inmail, body_text=reply_message)
            except Exception as e:
                print "Error {}".format(e)
                continue
            self.db.emails.update_one({'_id':db_id}, {'$set': {'acknowledged':True}})
            print "New Quote: {}".format(inmail['content'])

    def send_out_samples(self, users=[], sample_size=1):
        for user in users:
            print "Sending sample to {}".format(user)
            quotes = np.array([ unicode(quote['content']) for quote in self.db.quotes.find({'user':user}) ])
            sample = np.random.choice(quotes, size=sample_size, replace=False)
            text = (u"-"*50+u"\n").join(list(sample))
            try:
                self.send_email(user, "Daily Personal Quote", text)
            except Exception as e:
                print " [WARNING] Failed to send sample to user {}".format(user)
                print "   Error msg: {}".format(e)

    def remind(self, *_):
        print "Sending reminders"
        users = { datum['user'] for datum in self.db.quotes.find() }
        self.send_out_samples(users)


    def quit(self):
        self.sendserver.quit()
        self.inbox.quit()


if __name__ == '__main__':
    email_config = {
        "EMAIL_ORG": "@gmail.com",
        "FROM_UID":"personal.quote.service",
        "FROM_PWD": "thisisareallylongpassword",
        "IMAP_SERVER":"imap.gmail.com",
        "SMTP_SERVER": "smtp.gmail.com",
        "SMTP_PORT": 587
    }
    app = Server('quotes.db', email_config)
    s = sched.scheduler(time.time, time.sleep)
    while True:
        s.enter(5, 1, app.check_mail, ('',))
        s.enter(30, 2, app.remind, ('',))
        s.run()
