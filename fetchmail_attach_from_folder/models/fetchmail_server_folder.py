# Copyright - 2013-2018 Therp BV <https://therp.nl>.
# Copyright - 2020 Aures Tic Consultors S.L <https://www.aurestic.es>.
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
import base64
import email
import email.policy
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .. import match_algorithm

_logger = logging.getLogger(__name__)


class FetchmailServerFolder(models.Model):
    _name = "fetchmail.server.folder"
    _rec_name = "path"
    _order = "sequence"

    server_id = fields.Many2one(comodel_name="fetchmail.server", string="Server")
    sequence = fields.Integer(string="Sequence")
    state = fields.Selection(
        selection=[("draft", "Not Confirmed"), ("done", "Confirmed")],
        string="Status",
        readonly=True,
        required=True,
        copy=False,
        default="draft",
    )
    path = fields.Char(
        string="Path",
        required=True,
        help="The path to your mail folder."
        " Typically would be something like 'INBOX.myfolder'",
    )
    model_id = fields.Many2one(
        comodel_name="ir.model",
        string="Model",
        required=True,
        help="The model to attach emails to",
        ondelete="cascade",
    )
    model_field = fields.Char(
        string="Field (model)",
        help="The field in your model that contains the field to match "
        "against.\nExamples:\n"
        "'email' if your model is res.partner, or "
        "'partner_id.email' if you're matching sale orders",
    )
    model_order = fields.Char(
        string="Order (model)",
        help="Field(s) to order by, this mostly useful in conjunction "
        "with 'Use 1st match'",
    )
    match_algorithm = fields.Selection(
        selection=lambda s: s._get_match_algorithms_sel(),
        string="Match algorithm",
        required=True,
        help="The algorithm used to determine which object an email matches.",
    )
    mail_field = fields.Char(
        string="Field (email)",
        help="The field in the email used for matching. Typically "
        "this is 'to' or 'from'",
    )
    delete_matching = fields.Boolean(
        string="Delete matches", help="Delete matched emails from server"
    )
    flag_nonmatching = fields.Boolean(
        string="Flag nonmatching",
        default=True,
        help="Flag emails in the server that don't match any object in Odoo",
    )
    match_first = fields.Boolean(
        string="Use 1st match",
        help="If there are multiple matches, use the first one. If "
        "not checked, multiple matches count as no match at all",
    )
    domain = fields.Char(
        string="Domain", help="Fill in a search filter to narrow down objects to match"
    )
    msg_state = fields.Selection(
        selection=[("sent", "Sent"), ("received", "Received")],
        string="Message state",
        default="received",
        help="The state messages fetched from this folder should be "
        "assigned in Odoo",
    )
    active = fields.Boolean(string="Active", default=True)
    action_id = fields.Many2one(
        comodel_name="ir.actions.server",
        name="Server action",
        help="Optional custom server action to trigger for each incoming "
        "mail, on the record that was created or updated by this mail",
    )

    @api.model
    def _get_match_algorithms(self):
        def get_all_subclasses(cls):
            return cls.__subclasses__() + [
                subsub
                for sub in cls.__subclasses__()
                for subsub in get_all_subclasses(sub)
            ]

        return {
            cls.__name__: cls for cls in get_all_subclasses(match_algorithm.base.Base)
        }

    @api.model
    def _get_match_algorithms_sel(self):
        algorithms = []
        for cls in list(self._get_match_algorithms().values()):
            algorithms.append((cls.__name__, cls.name))
        algorithms.sort()
        return algorithms

    def get_algorithm(self):
        return self._get_match_algorithms()[self.match_algorithm]()

    def button_confirm_folder(self):
        for this in self:
            this.write({"state": "draft"})
            if not this.active:
                continue
            connection = this.server_id.connect()
            connection.select()
            if connection.select(this.path)[0] != "OK":
                raise ValidationError(_("Invalid folder %s!") % this.path)
            connection.close()
            this.write({"state": "done"})

    def button_attach_mail_manually(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "fetchmail.attach.mail.manually",
            "target": "new",
            "context": dict(self.env.context, folder_id=self.id),
            "view_mode": "form",
        }

    def set_draft(self):
        self.write({"state": "draft"})
        return True

    def get_msgids(self, connection, criteria):
        """Return imap ids of messages to process"""
        self.ensure_one()
        server = self.server_id
        _logger.info(
            "start checking for emails in folder %s on server %s",
            self.path,
            server.name,
        )
        if connection.select(self.path)[0] != "OK":
            raise UserError(
                _("Could not open mailbox %s on %s") % (self.path, server.name)
            )
        result, msgids = connection.search(None, criteria)
        if result != "OK":
            raise UserError(
                _("Could not search mailbox %s on %s") % (self.path, server.name)
            )
        _logger.info(
            "finished checking for emails in %s on server %s", self.path, server.name
        )
        return msgids

    def fetch_msg(self, connection, msgid):
        """Select a single message from a folder."""
        self.ensure_one()
        server = self.server_id
        result, msgdata = connection.fetch(msgid, "(RFC822)")
        if result != "OK":
            raise UserError(
                _("Could not fetch %s in %s on %s") % (msgid, self.path, server.server)
            )
        message_org = msgdata[0][1]  # rfc822 message source
        message = email.message_from_string(str(message_org), policy=email.policy.SMTP)
        mail_message = self.env["mail.thread"].message_parse(
            message, save_original=server.original
        )
        return (mail_message, message_org)

    def retrieve_imap_folder(self, connection):
        """Retrieve all mails for one IMAP folder."""
        self.ensure_one()
        msgids = self.get_msgids(connection, "UNSEEN UNDELETED")
        match_algorithm = self.get_algorithm()
        for msgid in msgids[0].split():
            # We will accept exceptions for single messages
            try:
                self.env.cr.execute("savepoint apply_matching")
                self.apply_matching(connection, msgid, match_algorithm)
                self.env.cr.execute("release savepoint apply_matching")
            except Exception:
                self.env.cr.execute("rollback to savepoint apply_matching")
                _logger.exception(
                    "Failed to fetch mail %s from %s", msgid, self.server_id.name
                )

    def fetch_mail(self):
        """Retrieve all mails for IMAP folders.

        We will use a separate connection for each folder.
        """
        for this in self:
            if not this.active or this.state != "done":
                continue
            connection = None
            try:
                # New connection per folder
                connection = this.server_id.connect()
                this.retrieve_imap_folder(connection)
                connection.close()
            except Exception:
                _logger.error(
                    _("General failure when trying to connect to %s server %s."),
                    this.server_id.server_type,
                    this.server_id.name,
                    exc_info=True,
                )
            finally:
                if connection:
                    connection.logout()

    def update_msg(self, connection, msgid, matched=True, flagged=False):
        """Update msg in imap folder depending on match and settings."""
        if matched:
            if self.delete_matching:
                connection.store(msgid, "+FLAGS", "\\DELETED")
            elif flagged and self.flag_nonmatching:
                connection.store(msgid, "-FLAGS", "\\FLAGGED")
        else:
            if self.flag_nonmatching:
                connection.store(msgid, "+FLAGS", "\\FLAGGED")

    def apply_matching(self, connection, msgid, match_algorithm):
        """Return ids of objects matched"""
        self.ensure_one()
        mail_message, message_org = self.fetch_msg(connection, msgid)
        if self.env["mail.message"].search(
            [("message_id", "=", mail_message["message_id"])]
        ):
            # Ignore mails that have been handled already
            return
        matches = match_algorithm.search_matches(self, mail_message)
        matched = matches and (len(matches) == 1 or self.match_first)
        if matched:
            thread_id = match_algorithm.handle_match(
                connection, matches[0], self, mail_message, message_org, msgid
            )
            self.run_server_action(thread_id)
        self.update_msg(connection, msgid, matched=matched)

    def run_server_action(self, matched_object_ids):
        self.ensure_one()
        action = self.action_id
        if not action:
            return
        records = self.env[self.model_id.model].browse(matched_object_ids)
        for record in records:
            if not record.exists():
                continue
            action.with_context(
                **{
                    "active_id": record.id,
                    "active_ids": record.ids,
                    "active_model": self.model_id.model,
                }
            ).run()

    def attach_mail(self, match_object, mail_message):
        """Attach mail to match_object."""
        self.ensure_one()
        partner = False
        model_name = self.model_id.model
        if model_name == "res.partner":
            partner = match_object
        elif "partner_id" in self.env[model_name]._fields:
            partner = match_object.partner_id
        attachments = []
        if self.server_id.attach and mail_message.get("attachments"):
            for attachment in mail_message["attachments"]:
                # Attachment should at least have filename and data, but
                # might have some extra element(s)
                if len(attachment) < 2:
                    continue
                fname, fcontent = attachment[:2]
                if isinstance(fcontent, str):
                    fcontent = fcontent.encode("utf-8")
                data_attach = {
                    "name": fname,
                    "datas": base64.b64encode(bytes(fcontent)),
                    "store_fname": fname,
                    "description": _("Mail attachment"),
                    "res_model": model_name,
                    "res_id": match_object.id,
                }
                attachments.append(self.env["ir.attachment"].create(data_attach))
        self.env["mail.message"].create(
            {
                "author_id": partner and partner.id or False,
                "model": model_name,
                "res_id": match_object.id,
                "message_type": "email",
                "body": mail_message.get("body"),
                "subject": mail_message.get("subject"),
                "email_from": mail_message.get("from"),
                "date": mail_message.get("date"),
                "message_id": mail_message.get("message_id"),
                "attachment_ids": [(6, 0, [a.id for a in attachments])],
            }
        )
