# -*- coding: utf-8 -*-
import logging
from collections import defaultdict

from odoo import api, models

_logger = logging.getLogger(__name__)


class HrVersion(models.Model):
    _inherit = "hr.version"

    def _get_version_work_entries_values(self, date_start, date_stop):
        """
        - Keep Odoo default work entries generation (duration logic unchanged)
        - For Attendance work entries:
            if the matching hr.attendance has N punch directions,
            create N work-entry values with mapped work_entry_type_id (external_code match),
            using SAME date_start/date_stop (so duration stays Odoo-default).
        - Unmatched direction -> skip
        - If nothing matched -> keep original Attendance entry
        """
        vals_list = super()._get_version_work_entries_values(date_start, date_stop)
        if not vals_list:
            return vals_list

        # Map: external_code(lower) -> work_entry_type_id
        we_type_map = {
            (w.external_code or "").strip().lower(): w.id
            for w in self.env["hr.work.entry.type"].sudo().search([("external_code", "!=", False)])
        }

        # Attendance default work entry type
        attendance_type = self.env.ref("hr_work_entry.work_entry_type_attendance", raise_if_not_found=False)
        attendance_type_id = attendance_type.id if attendance_type else False
        if not attendance_type_id:
            return vals_list

        # Collect employees present in vals_list
        emp_ids = list({v.get("employee_id") for v in vals_list if v.get("employee_id")})
        if not emp_ids:
            return vals_list

        # Fetch attendances overlapping the same window
        Attendance = self.env["hr.attendance"].sudo()
        attendances = Attendance.search([
            ("employee_id", "in", emp_ids),
            ("check_in", "<=", date_stop),
            "|",
            ("check_out", "=", False),
            ("check_out", ">=", date_start),
        ])

        # Index attendances by employee for quick match
        atts_by_emp = defaultdict(list)
        for a in attendances:
            atts_by_emp[a.employee_id.id].append(a)

        new_vals = []
        for vals in vals_list:
            # Only rewrite Attendance work entries that have date_start/date_stop
            if vals.get("work_entry_type_id") != attendance_type_id:
                new_vals.append(vals)
                continue

            if not vals.get("employee_id") or not vals.get("date_start") or not vals.get("date_stop"):
                new_vals.append(vals)
                continue

            emp_id = vals["employee_id"]
            ws = vals["date_start"]  # UTC-naive (core expects this)
            we = vals["date_stop"]

            # Find the first attendance that overlaps this interval
            matched_att = None
            for att in atts_by_emp.get(emp_id, []):
                ci = att.check_in
                co = att.check_out or we  # open attendance -> treat as overlapping
                # overlap check: (ci < we) and (co > ws)
                if ci and ci < we and co > ws:
                    matched_att = att
                    break

            # If no attendance found, keep original
            if not matched_att:
                new_vals.append(vals)
                continue

            # If no directions, keep original
            if not hasattr(matched_att, "direction_id") or not matched_att.direction_id:
                new_vals.append(vals)
                continue

            new_vals.append(vals)

            for direction in matched_att.direction_id:
                dcode = (direction.code or "").strip().lower()
                if not dcode:
                    continue

                we_type_id = we_type_map.get(dcode)
                if not we_type_id:
                    # no external_code match -> skip
                    continue

                v2 = vals.copy()
                v2["work_entry_type_id"] = we_type_id

                new_vals.append(v2)

        return new_vals