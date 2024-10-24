# Copyright - 2013-2018 Therp BV <https://therp.nl>.
# Copyright - 2020 Aures Tic Consultors S.L <https://www.aurestic.es>.
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
import json
import logging
import re

from lxml import etree

from odoo import _, api, fields, models
from odoo.tools.misc import UnquoteEvalContext
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)

list_response_pattern = re.compile(
    r'\((?P<flags>.*?)\) "(?P<delimiter>.*)" (?P<name>.*)'
)


class FetchmailServer(models.Model):
    _inherit = "fetchmail.server"

    folders_available = fields.Text(
        string="Available folders", compute="_compute_folders_available", readonly=True
    )
    folder_ids = fields.One2many(
        comodel_name="fetchmail.server.folder",
        inverse_name="server_id",
        string="Folders",
        context={"active_test": False},
    )
    object_id = fields.Many2one(required=False)  # comodel_name='ir.model'
    server_type = fields.Selection(default="imap")
    folders_only = fields.Boolean(
        string="Only folders, not inbox",
        help="Check this field to leave imap inbox alone"
        " and only retrieve mail from configured folders.",
    )

    def _compute_folders_available(self):
        """Retrieve available folders from IMAP server."""

        def parse_list_response(line):
            flags, delimiter, mailbox_name = list_response_pattern.match(
                line.decode("utf-8")
            ).groups()
            mailbox_name = mailbox_name.strip('"')
            return (flags, delimiter, mailbox_name)

        for this in self:
            if this.state != "done":
                this.folders_available = _("Confirm connection first.")
                continue
            connection = this.connect()
            list_result = connection.list()
            if list_result[0] != "OK":
                this.folders_available = _("Unable to retrieve folders.")
                continue
            folders_available = []
            for folder_entry in list_result[1]:
                folders_available.append(parse_list_response(folder_entry)[2])
            this.folders_available = "\n".join(folders_available)
            connection.logout()

    @api.onchange("server_type", "is_ssl", "object_id")
    def onchange_server_type(self):
        super(FetchmailServer, self).onchange_server_type()
        self.state = "draft"

    def fetch_mail(self):
        for this in self:
            if not this.folders_only:
                super(FetchmailServer, this).fetch_mail()
            this.folder_ids.fetch_mail()

    @api.model
    def fields_view_get(
        self, view_id=None, view_type="form", toolbar=False, submenu=False
    ):
        """Set modifiers for form fields in folder_ids depending on algorithm.

        A field will be readonly and/or required if this is specified in the
        algorithm.
        """
        result = super(FetchmailServer, self).fields_view_get(
            view_id=view_id, view_type=view_type, toolbar=toolbar, submenu=submenu
        )
        if view_type == "form":
            view = etree.fromstring(
                result["fields"]["folder_ids"]["views"]["form"]["arch"]
            )
            modifiers = {}
            docstr = ""
            folder_model = self.env["fetchmail.server.folder"]
            match_algorithms = folder_model._get_match_algorithms()
            for algorithm in list(match_algorithms.values()):
                for modifier in ["required", "readonly"]:
                    for field in getattr(algorithm, modifier + "_fields"):
                        modifiers.setdefault(field, {})
                        modifiers[field].setdefault(modifier, [])
                        if modifiers[field][modifier]:
                            modifiers[field][modifier].insert(0, "|")
                        modifiers[field][modifier].append(
                            ("match_algorithm", "==", algorithm.__name__)
                        )
                docstr += _(algorithm.name) + "\n" + _(algorithm.__doc__) + "\n\n"
            for field in view.xpath("//field"):
                if field.tag == "field" and field.get("name") in modifiers:
                    patched_modifiers = (
                        field.attrib["modifiers"]
                        .replace("false", "False")
                        .replace("true", "True")
                    )
                    original_dict = safe_eval(
                        patched_modifiers, UnquoteEvalContext({}), nocopy=True
                    )
                    modifier_dict = modifiers[field.attrib["name"]]
                    combined_dict = dict(original_dict, **modifier_dict)
                    field.set("modifiers", json.dumps(combined_dict))
                if field.tag == "field" and field.get("name") == "match_algorithm":
                    field.set("help", docstr)
            result["fields"]["folder_ids"]["views"]["form"]["arch"] = etree.tostring(
                view
            )
        return result
