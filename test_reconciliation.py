"""固定の架空データだけで照合を検証する。COM・通信・API取得は行わない。

実行: python test_reconciliation.py
依存: Python標準ライブラリとreconciliation.pyのみ。
"""

from copy import deepcopy
import unittest

from reconciliation import reconcile_racecard


FIELDS = ("horse_number", "horse_name", "frame_number", "jockey_name")
HORSE_A = {
    "horse_number": 1,
    "horse_name": "テストアルファ",
    "frame_number": 1,
    "jockey_name": "外部騎手",
}
HORSE_B = {
    "horse_number": 2,
    "horse_name": "テストベータ",
    "frame_number": 2,
    "jockey_name": "別騎手",
}


def horse_a(**changes):
    horse = deepcopy(HORSE_A)
    horse.update(changes)
    return horse


def racecard(horses, *, ocr=False):
    race = {"course": "東京", "race_number": 11}
    race["date" if ocr else "race_date"] = "20261003"
    return {"race": race, "horses": deepcopy(horses)}


class ReconciliationTests(unittest.TestCase):
    def reconcile(self, ocr_horses, external_horses, *, auto=False, ocr_race_changes=None):
        ocr = racecard(ocr_horses, ocr=True)
        external = racecard(external_horses)
        if ocr_race_changes:
            ocr["race"].update(ocr_race_changes)
        before_ocr, before_external = deepcopy(ocr), deepcopy(external)
        result = reconcile_racecard(ocr, external, auto_correct_by_horse_number=auto)
        # すべてのケースで、入力不変と双方の原本保持を確認する。
        self.assertEqual(ocr, before_ocr)
        self.assertEqual(external, before_external)
        self.assertEqual(result["originals"]["ocr"], before_ocr)
        self.assertEqual(result["originals"]["external"], before_external)
        self.assertIsNot(result["originals"]["ocr"], ocr)
        self.assertIsNot(result["originals"]["external"], external)
        for row in result["reconciliations"]:
            self.assertEqual(set(row["fields"]), set(FIELDS))
            for field in row["fields"].values():
                self.assertEqual(set(field), {"ocr_value", "external_value", "final_value", "status"})
        return result

    def ocr_row(self, result, index=0):
        return next(row for row in result["reconciliations"] if row["ocr_index"] == index)

    def assert_pending_reason(self, result, row_id, code):
        item = next(item for item in result["pending_review"] if item["row_id"] == row_id)
        self.assertTrue(item["fields"])
        self.assertIn(code, [reason["code"] for reason in item["reasons"]])
        for reason in item["reasons"]:
            self.assertTrue(reason["message"])

    def assert_unconfirmed(self, result, index=0):
        row = self.ocr_row(result, index)
        self.assertEqual(row["status"], "needs_review")
        self.assertFalse(row["identity_confirmed"])
        for field in row["fields"].values():
            self.assertEqual(field["status"], "needs_review")
            self.assertIsNone(field["final_value"])
        self.assertFalse(result["ready_for_scoring"])

    def test_01_exact_number_and_name_match(self):
        result = self.reconcile([HORSE_A], [HORSE_A])
        row = self.ocr_row(result)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(row["status"], "matched")
        self.assertTrue(row["identity_confirmed"])
        for field in FIELDS:
            self.assertEqual(row["fields"][field], {
                "ocr_value": HORSE_A[field], "external_value": HORSE_A[field],
                "final_value": HORSE_A[field], "status": "matched",
            })
        self.assertEqual(result["horses"], [HORSE_A])
        self.assertEqual(result["pending_review"], [])
        self.assertTrue(result["ready_for_scoring"])

    def test_02_name_spacing_and_width_are_corrected(self):
        for name in (" テスト アルファ　", "ﾃｽﾄｱﾙﾌｧ", " ﾃｽﾄ ｱﾙﾌｧ　"):
            with self.subTest(ocr_name=name):
                result = self.reconcile([horse_a(horse_name=name)], [HORSE_A])
                row = self.ocr_row(result)
                self.assertEqual(row["status"], "corrected")
                self.assertEqual(result["status"], "corrected")
                self.assertEqual(row["fields"]["horse_name"], {
                    "ocr_value": name, "external_value": HORSE_A["horse_name"],
                    "final_value": HORSE_A["horse_name"], "status": "corrected",
                })
                self.assertEqual(result["horses"], [HORSE_A])
                self.assertFalse(result["pending_review"])
                self.assertTrue(result["ready_for_scoring"])

    def test_03_different_name_and_similar_name_need_review(self):
        # 1文字違いの似た馬名も、類似度で自動確定しない。
        for name in ("まったく別の馬", "テストアルフア"):
            with self.subTest(ocr_name=name):
                result = self.reconcile([horse_a(horse_name=name)], [HORSE_A])
                self.assert_unconfirmed(result)
                self.assertEqual(result["horses"], [])
                self.assert_pending_reason(result, "ocr:0", "horse_name_mismatch")

    def test_04_duplicate_ocr_number_blocks_both_rows(self):
        result = self.reconcile([HORSE_A, horse_a(horse_name="テストベータ")], [HORSE_A])
        for index in (0, 1):
            self.assert_unconfirmed(result, index)
            self.assert_pending_reason(result, f"ocr:{index}", "duplicate_ocr_number")
        self.assertEqual(result["horses"], [])
        self.assertEqual(result["summary"]["ocr_count"], 2)

    def test_05_number_absent_from_external_needs_review(self):
        # 馬名だけが一致しても、馬番2を馬番1に変更しない。
        result = self.reconcile([horse_a(horse_number=2)], [HORSE_A])
        self.assert_unconfirmed(result)
        self.assertEqual(result["horses"], [])
        self.assert_pending_reason(result, "ocr:0", "number_not_in_external")
        self.assertEqual(self.ocr_row(result)["name_candidate_indices"], [0])

    def test_06_external_horse_missing_from_ocr_is_external_only(self):
        result = self.reconcile([HORSE_A], [HORSE_A, HORSE_B])
        row = next(row for row in result["reconciliations"] if row["row_id"] == "external:1")
        self.assertEqual(row["status"], "external_only")
        self.assertFalse(row["identity_confirmed"])
        for field in FIELDS:
            self.assertEqual(row["fields"][field], {
                "ocr_value": None, "external_value": HORSE_B[field],
                "final_value": None, "status": "external_only",
            })
        self.assertEqual(result["horses"], [HORSE_A])
        self.assert_pending_reason(result, "external:1", "external_only")
        self.assertFalse(result["ready_for_scoring"])

    def test_07_frame_and_jockey_use_external_after_confirmation(self):
        ocr_horse = horse_a(frame_number=8, jockey_name="OCR騎手")
        result = self.reconcile([ocr_horse], [HORSE_A])
        row = self.ocr_row(result)
        self.assertTrue(row["identity_confirmed"])
        for field in ("frame_number", "jockey_name"):
            self.assertEqual(row["fields"][field], {
                "ocr_value": ocr_horse[field], "external_value": HORSE_A[field],
                "final_value": HORSE_A[field], "status": "corrected",
            })
        self.assertEqual(result["horses"], [HORSE_A])
        self.assertTrue(result["ready_for_scoring"])

    def test_08_frame_and_jockey_remain_unfinalized_without_identity(self):
        ocr_horse = horse_a(horse_name="別馬", frame_number=8, jockey_name="OCR騎手")
        result = self.reconcile([ocr_horse], [HORSE_A])
        self.assert_unconfirmed(result)
        self.assertEqual(result["horses"], [])
        for field in ("frame_number", "jockey_name"):
            values = self.ocr_row(result)["fields"][field]
            self.assertEqual(values["ocr_value"], ocr_horse[field])
            self.assertEqual(values["external_value"], HORSE_A[field])
            self.assertIsNone(values["final_value"])
        self.assert_pending_reason(result, "ocr:0", "horse_name_mismatch")

    def test_09_partial_confirmation_does_not_enable_scoring(self):
        result = self.reconcile([HORSE_A, {**HORSE_B, "horse_name": "別馬"}], [HORSE_A, HORSE_B])
        self.assertEqual(result["horses"], [HORSE_A])
        self.assert_unconfirmed(result, 1)
        self.assertEqual(result["status"], "needs_review")
        self.assert_pending_reason(result, "ocr:1", "horse_name_mismatch")

    def test_10_preserves_ocr_details_and_independent_originals(self):
        ocr_horse = horse_a(
            training={"comment": "OCR調教コメント", "times": [12.3, 11.8]},
            previous_races=[{"race_date": "20260901", "comment": "OCR近走コメント"}],
            comment="OCR原文",
        )
        result = self.reconcile([ocr_horse], [HORSE_A])
        self.assertEqual(result["horses"], [ocr_horse])
        result["horses"][0]["training"]["times"][0] = 99.9
        self.assertEqual(result["originals"]["ocr"]["horses"][0], ocr_horse)
        result["originals"]["ocr"]["horses"][0]["previous_races"][0]["comment"] = "変更"
        self.assertEqual(ocr_horse["previous_races"][0]["comment"], "OCR近走コメント")

    def test_11_confirmed_horses_are_sorted_by_number(self):
        result = self.reconcile([HORSE_B, HORSE_A], [HORSE_A, HORSE_B])
        self.assertEqual(result["horses"], [HORSE_A, HORSE_B])
        self.assertTrue(result["ready_for_scoring"])
        self.assertEqual(result["pending_review"], [])

    def test_12_missing_fields_need_review_even_with_confirmed_identity(self):
        missing = horse_a(frame_number=None, jockey_name=None)
        result = self.reconcile([missing], [missing])
        row = self.ocr_row(result)
        self.assertTrue(row["identity_confirmed"])
        self.assertEqual(row["status"], "needs_review")
        self.assertEqual(result["horses"], [])
        self.assert_pending_reason(result, "ocr:0", "unresolved_fields")
        self.assertFalse(result["ready_for_scoring"])

    def test_13_auto_corrects_name_with_verified_numbers(self):
        wrong = horse_a(horse_name="OCR誤読馬名")
        result = self.reconcile([wrong, HORSE_B], [HORSE_B, HORSE_A], auto=True)
        self.assertEqual(result["auto_correction"], {
            "requested": True, "enabled": True,
            "reason": "all_horse_numbers_verified", "failures": [],
        })
        self.assertEqual(result["horses"], [HORSE_A, HORSE_B])
        row = self.ocr_row(result)
        self.assertEqual(row["status"], "corrected")
        self.assertEqual(row["fields"]["horse_name"]["ocr_value"], "OCR誤読馬名")
        self.assertEqual(row["fields"]["horse_name"]["final_value"], HORSE_A["horse_name"])
        self.assertIn("horse_number_verified_auto_correction", [reason["code"] for reason in row["reasons"]])
        self.assertEqual(self.ocr_row(result, 1)["status"], "matched")
        self.assertEqual(result["pending_review"], [])
        self.assertTrue(result["ready_for_scoring"])

    def assert_auto_disabled(self, result, failure):
        self.assertTrue(result["auto_correction"]["requested"])
        self.assertFalse(result["auto_correction"]["enabled"])
        self.assertIn(failure, result["auto_correction"]["failures"])
        self.assertTrue(result["auto_correction"]["reason"])
        self.assertFalse(result["ready_for_scoring"])
        self.assertTrue(result["pending_review"])

    def test_14_auto_disabled_for_duplicate_ocr_number(self):
        result = self.reconcile([HORSE_A, horse_a(horse_name="OCR誤読")], [HORSE_A, HORSE_B], auto=True)
        self.assert_auto_disabled(result, "duplicate_ocr_horse_number")
        self.assert_unconfirmed(result, 0)
        self.assert_unconfirmed(result, 1)

    def test_15_auto_disabled_for_missing_ocr_number(self):
        result = self.reconcile([horse_a(horse_number=None)], [HORSE_A], auto=True)
        self.assert_auto_disabled(result, "ocr_horse_number_missing_or_invalid")
        self.assert_unconfirmed(result)

    def test_16_auto_disabled_globally_for_insufficient_ocr_count(self):
        result = self.reconcile([horse_a(horse_name="OCR誤読")], [HORSE_A, HORSE_B], auto=True)
        self.assert_auto_disabled(result, "horse_count_mismatch")
        self.assert_unconfirmed(result)
        self.assertEqual(result["horses"], [])

    def test_17_auto_disabled_for_race_mismatch_or_missing_metadata(self):
        for changes, failure in (
            ({"date": "20261004"}, "race_mismatch"),
            ({"course": "中山"}, "race_mismatch"),
            ({"race_number": 10}, "race_mismatch"),
            ({"date": None}, "race_information_incomplete"),
        ):
            with self.subTest(changes=changes):
                result = self.reconcile([horse_a(horse_name="OCR誤読")], [HORSE_A],
                                        auto=True, ocr_race_changes=changes)
                self.assert_auto_disabled(result, failure)
                self.assert_unconfirmed(result)

    def test_18_default_false_keeps_conservative_behavior(self):
        ocr = racecard([horse_a(horse_name="OCR誤読")], ocr=True)
        external = racecard([HORSE_A])
        default = reconcile_racecard(ocr, external)
        explicit = reconcile_racecard(ocr, external, auto_correct_by_horse_number=False)
        self.assertEqual(default, explicit)
        self.assertEqual(default["auto_correction"]["reason"], "not_requested")
        self.assert_unconfirmed(default)
        self.assertEqual(default["horses"], [])

    def test_19_auto_preserves_all_other_ocr_fields(self):
        wrong = horse_a(horse_name="OCR誤読", frame_number=8, jockey_name="OCR騎手",
                        training={"comment": "調教原文", "times": [11.8]},
                        previous_races=[{"comment": "近走原文"}], running_style="先行",
                        comment="コメント原文", pedigree={"pedigree_note": "血統評価原文"},
                        other_ocr_field={"text": "その他原文"})
        result = self.reconcile([wrong], [HORSE_A], auto=True)
        expected = deepcopy(wrong)
        expected.update(HORSE_A)
        self.assertEqual(result["horses"], [expected])
        for field in ("horse_name", "frame_number", "jockey_name"):
            values = self.ocr_row(result)["fields"][field]
            self.assertEqual(values["ocr_value"], wrong[field])
            self.assertEqual(values["external_value"], HORSE_A[field])
            self.assertEqual(values["final_value"], HORSE_A[field])
            self.assertEqual(values["status"], "corrected")
        self.assertTrue(result["ready_for_scoring"])
        result["horses"][0]["training"]["times"][0] = 99.9
        self.assertEqual(result["originals"]["ocr"]["horses"][0], wrong)

    def test_20_auto_rejects_string_bool_and_unknown_numbers(self):
        for number, failure in (("1", "ocr_horse_numbers_not_all_integers"),
                                (True, "ocr_horse_numbers_not_all_integers"),
                                (3, "ocr_horse_number_not_in_external")):
            with self.subTest(number=number):
                result = self.reconcile([horse_a(horse_number=number, horse_name="OCR誤読")],
                                        [HORSE_A], auto=True)
                self.assert_auto_disabled(result, failure)
                self.assert_unconfirmed(result)

    def test_21_auto_rejects_external_number_duplicates(self):
        result = self.reconcile([HORSE_A, HORSE_B], [HORSE_A, HORSE_A], auto=True)
        self.assert_auto_disabled(result, "duplicate_external_horse_number")
        self.assert_unconfirmed(result, 0)

    def test_22_auto_does_not_fallback_to_ocr_for_missing_external_values(self):
        result = self.reconcile([horse_a(horse_name="OCR誤読")],
                                [horse_a(frame_number=None)], auto=True)
        self.assertTrue(result["auto_correction"]["enabled"])
        self.assertEqual(self.ocr_row(result)["status"], "needs_review")
        self.assertIsNone(self.ocr_row(result)["fields"]["frame_number"]["final_value"])
        self.assertEqual(result["horses"], [])
        self.assertFalse(result["ready_for_scoring"])
        self.assert_pending_reason(result, "ocr:0", "unresolved_fields")


if __name__ == "__main__":
    unittest.main(verbosity=2)
