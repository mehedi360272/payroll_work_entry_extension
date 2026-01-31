# -*- coding: utf-8 -*-
from collections import defaultdict
from datetime import datetime, time, timedelta
from itertools import groupby

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
from dateutil.relativedelta import relativedelta


class HrWorkEntryRegenerationWizard(models.TransientModel):
    _inherit = "hr.work.entry.regeneration.wizard"

    def _create_absent_from_planning(self, employees, date_from, date_to):
        WorkEntryType = self.env["hr.work.entry.type"].sudo()
        WorkEntry = self.env["hr.work.entry"].sudo()
        Slot = self.env["planning.slot"].sudo()
        Leave = self.env["hr.leave"].sudo()
        Attendance = self.env["hr.attendance"].sudo()

        absent_types = WorkEntryType.search([("external_code", "=", "absent")], limit=2)
        if not absent_types:
            raise ValidationError(_(
                "Work Entry Type with External Code = 'absent' is missing.\n"
                "Please create it to generate Absent work entries."
            ))
        if len(absent_types) > 1:
            raise ValidationError(
                _("Multiple Work Entry Types found with External Code = 'absent'. Please keep only one."))
        absent_type = absent_types[0]

        emp_ids = employees.ids
        if not emp_ids:
            return

        # slot search uses datetime
        dt_start = datetime.combine(date_from, time.min)
        dt_stop = datetime.combine(date_to, time.max)

        # ------------------------------------
        # A) Planning slots in range
        # ------------------------------------
        slot_domain = [
            ("employee_id", "in", emp_ids),
            ("start_datetime", "<", dt_stop),
            ("end_datetime", ">", dt_start),
        ]
        if "state" in Slot._fields:
            slot_domain.append(("state", "!=", "cancel"))

        slots = Slot.search(slot_domain)
        if not slots:
            return

        # merge slots -> one absent per (employee, day)
        slot_days_by_emp = defaultdict(set)
        for s in slots:
            if not s.employee_id or not s.start_datetime:
                continue
            day = s.start_datetime.date()
            slot_days_by_emp[s.employee_id.id].add(day)

        # ------------------------------------
        # B) Validated leave days preload
        # ------------------------------------
        leave_days_by_emp = defaultdict(set)
        leaves = Leave.search([
            ("employee_id", "in", emp_ids),
            ("state", "=", "validate"),
            ("request_date_from", "<=", date_to),
            ("request_date_to", ">=", date_from),
        ])
        for lv in leaves:
            d = lv.request_date_from
            while d and d <= lv.request_date_to:
                leave_days_by_emp[lv.employee_id.id].add(d)
                d += timedelta(days=1)

        # ------------------------------------
        # C) Attendance days preload (DAY BASED)
        #    - if any attendance exists on that day -> no absent
        # ------------------------------------
        att_days = set()
        atts = Attendance.search([
            ("employee_id", "in", emp_ids),
            ("check_in", "<", dt_stop),
            "|",
            ("check_out", "=", False),
            ("check_out", ">", dt_start),
        ])
        for att in atts:
            if att.check_in:
                att_days.add((att.employee_id.id, att.check_in.date()))

        # ------------------------------------
        # D) Duplicate prevent (DB)
        # ------------------------------------
        existing_abs = WorkEntry.search([
            ("employee_id", "in", emp_ids),
            ("work_entry_type_id", "=", absent_type.id),
            ("date", ">=", date_from),
            ("date", "<=", date_to),
        ])
        existing_abs_days = set((we.employee_id.id, we.date) for we in existing_abs if we.employee_id and we.date)

        today = fields.Date.context_today(self)
        has_contract_field = "contract_id" in WorkEntry._fields

        # ------------------------------------
        # E) Create ABSENT
        # ------------------------------------
        for emp_id, days in slot_days_by_emp.items():
            emp = self.env["hr.employee"].browse(emp_id)

            for day in sorted(days):
                if day > today:
                    continue

                # 1) leave থাকলে skip
                if day in leave_days_by_emp.get(emp_id, set()):
                    continue

                # 2) attendance থাকলে skip ✅ (new fix)
                if (emp_id, day) in att_days:
                    continue

                # 3) already absent exists -> skip
                if (emp_id, day) in existing_abs_days:
                    continue

                vals = {
                    "name": f"ABSENT ({emp.name})",
                    "employee_id": emp_id,
                    "work_entry_type_id": absent_type.id,
                    "date": day,
                }

                # contract optional (contract না পেলে skip)
                if has_contract_field:
                    we_contract = WorkEntry.search([
                        ("employee_id", "=", emp_id),
                        ("date", "=", day),
                    ], limit=1)
                    contract_id = we_contract.contract_id.id if (we_contract and we_contract.contract_id) else False
                    if not contract_id:
                        continue
                    vals["contract_id"] = contract_id

                WorkEntry.with_context(work_entry_skip_validation=True).create(vals)
                existing_abs_days.add((emp_id, day))

    def regenerate_work_entries(self, slots=None, record_ids=None):
        # 1) standard regeneration
        res = super().regenerate_work_entries(slots=slots, record_ids=record_ids)

        # 2) absent creation after regenerate
        if not slots:
            valid_employees = self.employee_ids - self.validated_work_entry_employee_ids
            date_from = max(self.date_from, self.earliest_available_date) if self.earliest_available_date else self.date_from
            date_to = min(self.date_to, self.latest_available_date) if self.latest_available_date else self.date_to
            self._create_absent_from_planning(valid_employees, date_from, date_to)
        else:
            # slots-mode ranges
            range_by_employee = defaultdict(list)
            slots.sort(key=lambda d: (d["employee_id"], d["date"]))
            for employee_id, records in groupby(slots, lambda d: d["employee_id"]):
                dates = [fields.Date.from_string(r["date"]) for r in records]
                start = end = dates[0]
                for current in dates[1:]:
                    if current - end != timedelta(days=1):
                        range_by_employee[start, end].append(employee_id)
                        start = current
                    end = current
                range_by_employee[start, end].append(employee_id)

            for (date_from, date_to), employee_ids in range_by_employee.items():
                emps = self.env["hr.employee"].browse(employee_ids)
                self._create_absent_from_planning(emps, date_from, date_to)

        return res
