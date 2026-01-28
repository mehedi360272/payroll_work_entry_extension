# -*- coding: utf-8 -*-
from datetime import datetime, time
from collections import defaultdict

from odoo import models, fields


class AzkPlanningSyncWizardInherit(models.TransientModel):
    _inherit = "azk.planning.sync.wizard"

    def action_sync(self):
        self.ensure_one()
        print("\n==============================")
        print("ACTION_SYNC CLICKED")
        print("==============================")

        # ✅ ALWAYS initialize (so never UnboundLocalError)
        processed_employees = self.env["hr.employee"]  # empty recordset
        overall_min = None
        overall_max = None

        # 1) run sync and get summary
        start_dt = datetime.combine(self.date_start, time.min)
        end_dt = datetime.combine(self.date_end, time.max)

        print("SYNC WINDOW:", start_dt, "->", end_dt)
        print("EMPLOYEE IDS (wizard):", self.employee_ids.ids)
        print("TOLERANCE (minutes):", self.tolerance_minutes)

        summary = self.env["azk.report.daily.attendance.filtered"]._sync_from_filtered(
            start_dt,
            end_dt,
            employee_ids=self.employee_ids.ids,
        )
        print("SYNC SUMMARY:", summary)

        # 2) ONLY processed slots -> generate work entries from those windows
        slot_ids = summary.get("slot_ids") or []
        print("SLOT IDS:", slot_ids)

        if slot_ids:
            Slot = self.env["planning.slot"].sudo()
            slots = Slot.browse(slot_ids).exists()
            print("SLOTS FOUND:", len(slots))

            emp_windows = defaultdict(lambda: {"min": None, "max": None})

            for s in slots:
                emp = getattr(s, "employee_id", False)
                print(
                    f"SLOT id={s.id} | emp={(emp.id if emp else None)} "
                    f"| start={s.start_datetime} | end={s.end_datetime}"
                )

                if not emp or not s.start_datetime or not s.end_datetime:
                    print("  -> SKIP: missing emp/start/end")
                    continue

                w = emp_windows[emp.id]
                w["min"] = s.start_datetime if not w["min"] else min(w["min"], s.start_datetime)
                w["max"] = s.end_datetime if not w["max"] else max(w["max"], s.end_datetime)

                overall_min = w["min"] if not overall_min else min(overall_min, w["min"])
                overall_max = w["max"] if not overall_max else max(overall_max, w["max"])

            print("EMP WINDOWS:", dict(emp_windows))

            WorkEntry = self.env["hr.work.entry"].sudo()
            processed_employees = self.env["hr.employee"].browse(list(emp_windows.keys())).exists()
            print("EMPLOYEES FOR GENERATE:", processed_employees.ids)

            # ✅ employee-wise generate (tight window)
            if hasattr(WorkEntry, "_generate_work_entries"):
                print("USING: hr.work.entry._generate_work_entries()")
                for emp in processed_employees:
                    w = emp_windows[emp.id]
                    print(f"GENERATE FOR emp={emp.id} window={w['min']} -> {w['max']}")
                    if w["min"] and w["max"]:
                        WorkEntry._generate_work_entries(w["min"], w["max"], employees=emp)
                        print("  -> CALLED _generate_work_entries ✅")
            else:
                print("FALLBACK: hr.employee._generate_work_entries()")
                for emp in processed_employees:
                    w = emp_windows[emp.id]
                    print(f"GENERATE FOR emp={emp.id} window={w['min']} -> {w['max']}")
                    if w["min"] and w["max"] and hasattr(emp, "_generate_work_entries"):
                        emp._generate_work_entries(w["min"], w["max"])
                        print("  -> CALLED emp._generate_work_entries ✅")

        print("WORK ENTRY PART FINISHED")
        print("==============================\n")

        # ✅✅ 3) regenerate AFTER finish (fallback: wizard date range + wizard employee_ids)
        regen_employees = processed_employees or self.employee_ids
        regen_min = overall_min or datetime.combine(self.date_start, time.min)
        regen_max = overall_max or datetime.combine(self.date_end, time.max)

        print(
            "REGEN DEBUG -> processed_employees:",
            processed_employees.ids,
            "| wizard employees:",
            self.employee_ids.ids,
            "| overall_min:",
            overall_min,
            "| overall_max:",
            overall_max,
        )

        if regen_employees and regen_min and regen_max:
            print(f"REGENERATE AFTER FINISH: employees={regen_employees.ids} window={regen_min} -> {regen_max}")

            wiz = self.env["hr.work.entry.regeneration.wizard"].sudo().create({
                "date_from": regen_min.date(),
                "date_to": regen_max.date(),
                "employee_ids": [(6, 0, regen_employees.ids)],
            })

            # skip validations for automation
            wiz = wiz.with_context(work_entry_skip_validation=True)
            wiz.regenerate_work_entries()
            print("\n==============================")
            print("  -> CALLED wizard.regenerate_work_entries ✅")
            print("==============================\n")
        else:
            print("REGENERATE SKIPPED (no employees/window even after fallback)")

        # ==========================================================
        # 4) CREATE ABSENT work entries (DAY-BASED: hr.work.entry.date)
        #    - Check planning slot
        #    - Check approved leave overlap
        #    - Check attendance overlap
        #    - External Code = 'ABS'
        # ==========================================================
        try:
            WorkEntryType = self.env["hr.work.entry.type"].sudo()
            WorkEntry = self.env["hr.work.entry"].sudo()

            if "date" not in WorkEntry._fields:
                print("ABSENT: hr.work.entry has no 'date' field. SKIP.")
                raise Exception("Missing hr.work.entry.date")

            has_contract_field = "contract_id" in WorkEntry._fields
            print("ABSENT: hr.work.entry has contract_id?", has_contract_field)

            # Find ABSENT type by external_code='ABS'
            absent_type = False
            if "external_code" in WorkEntryType._fields:
                absent_type = WorkEntryType.search([("external_code", "=", "ABS")], limit=1)
            else:
                dom = []
                if "display_code" in WorkEntryType._fields and "code" in WorkEntryType._fields:
                    dom = ["|", ("display_code", "=", "ABS"), ("code", "=", "ABSENT")]
                elif "code" in WorkEntryType._fields:
                    dom = [("code", "=", "ABSENT")]
                absent_type = WorkEntryType.search(dom, limit=1) if dom else False

            if not absent_type:
                print("ABSENT TYPE NOT FOUND (external_code='ABS'). SKIP.")
            else:
                emp_ids = regen_employees.ids if regen_employees else self.employee_ids.ids
                if not emp_ids:
                    print("ABSENT: NO EMPLOYEES -> SKIP.")
                else:
                    Slot = self.env["planning.slot"].sudo()
                    slot_domain = [
                        ("employee_id", "in", emp_ids),
                        ("start_datetime", "<", regen_max),
                        ("end_datetime", ">", regen_min),
                    ]
                    if "state" in Slot._fields:
                        slot_domain.append(("state", "!=", "cancel"))

                    slots = Slot.search(slot_domain)
                    print("ABSENT: ALL SLOTS FOUND IN WINDOW:", len(slots))

                    Attendance = self.env["hr.attendance"].sudo()
                    Leave = self.env["hr.leave"].sudo()

                    # preload attendances
                    att_by_emp = defaultdict(list)
                    atts = Attendance.search([
                        ("employee_id", "in", emp_ids),
                        ("check_in", "<", regen_max),
                        "|", ("check_out", "=", False), ("check_out", ">", regen_min),
                    ])
                    for a in atts:
                        att_by_emp[a.employee_id.id].append(a)

                    # merge slots -> one absent per (employee, day)
                    slot_days_by_emp = defaultdict(set)
                    slot_windows_by_emp_day = defaultdict(lambda: {"min": None, "max": None})

                    for s in slots:
                        emp = s.employee_id
                        if not emp or not s.start_datetime or not s.end_datetime:
                            continue
                        day = s.start_datetime.date()
                        slot_days_by_emp[emp.id].add(day)

                        key = (emp.id, day)
                        w = slot_windows_by_emp_day[key]
                        w["min"] = s.start_datetime if not w["min"] else min(w["min"], s.start_datetime)
                        w["max"] = s.end_datetime if not w["max"] else max(w["max"], s.end_datetime)

                    absent_created = 0
                    absent_skipped_future = 0
                    absent_skipped_leave = 0
                    absent_skipped_att = 0
                    absent_skipped_dup = 0
                    absent_skipped_contract = 0  # will stay 0 if no contract field
                    today = fields.Date.context_today(self)

                    for emp_id, days in slot_days_by_emp.items():
                        emp = self.env["hr.employee"].browse(emp_id)
                        for day in sorted(days):
                            if day > today:
                                absent_skipped_future += 1
                                continue
                            ws = slot_windows_by_emp_day[(emp_id, day)]["min"]
                            we = slot_windows_by_emp_day[(emp_id, day)]["max"]

                            # A) Approved leave on that day?
                            leave_exists = Leave.search_count([
                                ("employee_id", "=", emp_id),
                                ("state", "=", "validate"),
                                ("request_date_from", "<=", day),
                                ("request_date_to", ">=", day),
                            ])
                            if leave_exists:
                                absent_skipped_leave += 1
                                continue

                            # B) Attendance overlap within slot window
                            overlapped = False
                            for att in att_by_emp.get(emp_id, []):
                                ci = att.check_in
                                co = att.check_out or (we or datetime.combine(day, time.max))
                                if ws and we and ci and ci < we and co > ws:
                                    overlapped = True
                                    break
                                if ci and ci.date() == day:
                                    overlapped = True
                                    break
                            if overlapped:
                                absent_skipped_att += 1
                                continue

                            # C) Duplicate prevent (employee+date+type)
                            already = WorkEntry.search_count([
                                ("employee_id", "=", emp_id),
                                ("date", "=", day),
                                ("work_entry_type_id", "=", absent_type.id),
                            ])
                            if already:
                                absent_skipped_dup += 1
                                continue

                            # D) Build create vals
                            vals = {
                                "name": f"ABSENT ({emp.name})",
                                "employee_id": emp_id,
                                "work_entry_type_id": absent_type.id,
                                "date": day,
                            }

                            # Only if contract_id exists in this DB
                            if has_contract_field:
                                # Try to grab any existing work entry's contract on the same day
                                we_contract = WorkEntry.search([
                                    ("employee_id", "=", emp_id),
                                    ("date", "=", day),
                                ], limit=1)
                                contract_id = we_contract.contract_id.id if (
                                            we_contract and we_contract.contract_id) else False
                                if not contract_id:
                                    absent_skipped_contract += 1
                                    continue
                                vals["contract_id"] = contract_id

                            WorkEntry.with_context(work_entry_skip_validation=True).create(vals)
                            absent_created += 1

                    print(
                        "ABSENT SUMMARY -> created:", absent_created,
                        "| skip_leave:", absent_skipped_leave,
                        "| skip_att:", absent_skipped_att,
                        "| skip_dup:", absent_skipped_dup,
                        "| skip_contract:", absent_skipped_contract,
                    )

        except Exception as e:
            print("ABSENT CREATE ERROR:", repr(e))

        # 4) show notification
        message = (
            f"Processed {summary.get('slots', 0)} planning shifts.\n"
            f"Created: {summary.get('created', 0)} | Updated: {summary.get('updated', 0)} | Skipped: {summary.get('skipped', 0)}"
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Planning Sync",
                "message": message,
                "sticky": False,
                "type": "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
