# -*- encoding: utf-8 -*-

import imaplib
import time
from datetime import datetime

from openerp import api, fields, models


class FetchmailServerFolder(models.Model):
    _inherit = 'fetchmail.server'

    @api.multi
    def handle_folder(self, connection, folder):
        res = super(FetchmailServerFolder, self.with_context(
            folder=folder
        )).handle_folder(connection, folder)
        folder.last_internal_date = fields.Datetime.now()
        return res

    @api.multi
    def get_msgids(self, connection):
        folder = self._context.get("folder", self.env["fetchmail.server.folder"])
        msgids = []
        if folder.last_internal_date:
            last_internal_date = fields.Datetime.from_string(
                folder.last_internal_date
            )
            result, uids = connection.search(
                None,
                'SINCE', '%s' % last_internal_date.strftime('%d-%b-%Y')
            )
            new_uids = uids[0].split()
            for new_uid in new_uids:
                fetch_status, date = connection.fetch(
                    int(new_uid),
                    'INTERNALDATE'
                )
                internaldate = imaplib.Internaldate2tuple(date[0])
                internaldate_msg = datetime.fromtimestamp(
                    time.mktime(internaldate)
                )
                if internaldate_msg > last_internal_date:
                    msgids.append(str(new_uid))
        else:
            result, msgids = connection.search(None, 'UNDELETED')
        return result, [' '.join(msgids)]
