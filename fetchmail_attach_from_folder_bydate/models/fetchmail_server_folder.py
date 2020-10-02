# -*- encoding: utf-8 -*-

from openerp import models, fields


class FetchmailServerFolder(models.Model):
    _inherit = 'fetchmail.server.folder'

    last_internal_date = fields.Datetime(
        string='Last Download Date',
        help="Remote emails with a date greater than this will be "
             "downloaded. Only available with IMAP",
    )
