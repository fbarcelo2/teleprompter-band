"""Tests for the pure teleprompter logic.

These run headless: no Tk, no X11, no display required.
"""

import json
import os
import tempfile
import unittest

import core


class ParseScriptTest(unittest.TestCase):
    def test_empty_input_has_no_paragraphs(self):
        self.assertEqual(core.parse_script(""), [])
        self.assertEqual(core.parse_script("   \n\n \t\n"), [])

    def test_soft_wrapped_lines_join_into_one_paragraph(self):
        text = "Hola, este texto sigue\nen la proxima linea."
        self.assertEqual(
            core.parse_script(text), ["Hola, este texto sigue en la proxima linea."]
        )

    def test_blank_line_separates_paragraphs(self):
        text = "Primera parte.\n\nSegunda parte."
        self.assertEqual(core.parse_script(text), ["Primera parte.", "Segunda parte."])

    def test_heading_and_note_lines_are_not_read(self):
        text = "# Introduccion\nSe lee esto.\n> No leas esto.\n"
        self.assertEqual(core.parse_script(text), ["Se lee esto."])

    def test_crlf_input_is_normalized(self):
        self.assertEqual(core.parse_script("Uno\r\n\r\nDos"), ["Uno", "Dos"])

    def test_inner_whitespace_is_collapsed(self):
        self.assertEqual(core.parse_script("a    b\t\tc"), ["a b c"])


class CountWordsTest(unittest.TestCase):
    def test_counts_words_across_paragraphs(self):
        self.assertEqual(core.count_words(["uno dos", "tres"]), 3)

    def test_punctuation_glued_to_words_does_not_create_words(self):
        self.assertEqual(core.count_words(["hola, mundo."]), 2)

    def test_empty_script_has_zero_words(self):
        self.assertEqual(core.count_words([]), 0)


class VelocityTest(unittest.TestCase):
    def test_wpm_converts_through_words_per_pixel_of_the_real_script(self):
        # 100 words laid out over 1000 px -> 10 px per word.
        # 150 words/minute is 2.5 words/second -> 25 px per second.
        self.assertAlmostEqual(core.velocity_px_per_s(150, 1000, 100), 25.0)

    def test_zero_words_means_zero_speed(self):
        self.assertEqual(core.velocity_px_per_s(150, 1000, 0), 0.0)

    def test_negative_pace_is_clamped_to_zero(self):
        self.assertEqual(core.velocity_px_per_s(-10, 1000, 100), 0.0)


class AdvanceTest(unittest.TestCase):
    def test_offset_advances_linearly_with_time(self):
        self.assertAlmostEqual(core.advance(100, 40, 0.5, 1000), 120)
        self.assertAlmostEqual(core.advance(100, 40, 1.0, 1000), 140)

    def test_never_goes_below_zero(self):
        self.assertEqual(core.advance(10, -40, 1.0, 1000), 0.0)

    def test_never_goes_past_the_end_of_the_script(self):
        self.assertEqual(core.advance(990, 40, 1.0, 1000), 1000.0)

    def test_zero_delta_time_does_not_move(self):
        self.assertEqual(core.advance(100, 40, 0.0, 1000), 100.0)

    def test_paused_pacing_holds_position(self):
        self.assertEqual(core.advance(100, 0, 1.0, 1000), 100.0)


class PacingTest(unittest.TestCase):
    def test_faster_then_slower_returns_to_the_same_pace(self):
        pacing = core.Pacing(100.0, 0.10)
        pacing.faster()
        self.assertAlmostEqual(pacing.wpm, 110.0)
        pacing.slower()
        self.assertAlmostEqual(pacing.wpm, 100.0)

    def test_pace_is_clamped_at_both_ends(self):
        fast = core.Pacing(core.Pacing.MAX_WPM, 0.10)
        fast.faster()
        self.assertEqual(fast.wpm, core.Pacing.MAX_WPM)

        slow = core.Pacing(core.Pacing.MIN_WPM, 0.10)
        slow.slower()
        self.assertEqual(slow.wpm, core.Pacing.MIN_WPM)

    def test_label_rounds_for_display(self):
        self.assertEqual(core.Pacing(144.6).label(), "145 wpm")


class ProgressTest(unittest.TestCase):
    def test_progress_is_a_ratio_of_the_scroll_length(self):
        self.assertAlmostEqual(core.progress(250, 1000), 0.25)
        self.assertEqual(core.progress(0, 1000), 0.0)

    def test_progress_of_an_empty_script_is_zero(self):
        self.assertEqual(core.progress(0, 0), 0.0)

    def test_offset_for_progress_is_the_inverse(self):
        self.assertAlmostEqual(core.offset_for_progress(0.25, 1000), 250.0)

    def test_offset_for_progress_clamps_the_ratio(self):
        self.assertEqual(core.offset_for_progress(2.0, 1000), 1000.0)
        self.assertEqual(core.offset_for_progress(-1.0, 1000), 0.0)


XRANDR_SAMPLE = """Monitors: 2
 0: +*HDMI-1-0 3840/597x2160/336+0+0  HDMI-1-0
 1: +eDP-1 1920/344x1080/193+3840+0  eDP-1
"""


class MonitorsTest(unittest.TestCase):
    def test_parses_every_connected_monitor(self):
        monitors = core.parse_xrandr_monitors(XRANDR_SAMPLE)
        self.assertEqual([m["name"] for m in monitors], ["HDMI-1-0", "eDP-1"])

    def test_parses_geometry_of_the_laptop_panel(self):
        laptop = core.parse_xrandr_monitors(XRANDR_SAMPLE)[1]
        self.assertEqual(
            (laptop["width"], laptop["height"], laptop["x"], laptop["y"]),
            (1920, 1080, 3840, 0),
        )

    def test_unparsable_input_yields_no_monitors(self):
        self.assertEqual(core.parse_xrandr_monitors(""), [])
        self.assertEqual(core.parse_xrandr_monitors("Monitors: 0\n"), [])

    def test_resolve_by_name(self):
        monitors = core.parse_xrandr_monitors(XRANDR_SAMPLE)
        self.assertEqual(core.resolve_monitor("eDP-1", monitors)["x"], 3840)

    def test_resolve_unknown_name_returns_none(self):
        monitors = core.parse_xrandr_monitors(XRANDR_SAMPLE)
        self.assertIsNone(core.resolve_monitor("DP-9", monitors))


class PickMonitorTest(unittest.TestCase):
    def setUp(self):
        self.monitors = core.parse_xrandr_monitors(XRANDR_SAMPLE)

    def test_named_monitor_wins(self):
        self.assertEqual(core.pick_monitor("eDP-1", self.monitors)["width"], 1920)

    def test_empty_name_falls_back_to_the_first_monitor(self):
        self.assertEqual(core.pick_monitor("", self.monitors)["name"], "HDMI-1-0")

    def test_unknown_name_falls_back_to_the_first_monitor(self):
        self.assertEqual(core.pick_monitor("DP-9", self.monitors)["name"], "HDMI-1-0")

    def test_no_monitors_at_all_returns_none(self):
        self.assertIsNone(core.pick_monitor("eDP-1", []))


class SettingsTest(unittest.TestCase):
    def test_missing_file_yields_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = core.load_settings(os.path.join(tmp, "nope.json"))
            self.assertEqual(settings, core.DEFAULTS)

    def test_partial_file_merges_over_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"wpm": 120, "font_size": 40}, handle)
            settings = core.load_settings(path)
            self.assertEqual(settings["wpm"], 120)
            self.assertEqual(settings["font_size"], 40)
            self.assertEqual(settings["monitor"], core.DEFAULTS["monitor"])

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(core.load_settings(path), core.DEFAULTS)

    def test_save_creates_parent_directories_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deep", "config.json")
            core.save_settings(path, dict(core.DEFAULTS, wpm=133))
            self.assertEqual(core.load_settings(path)["wpm"], 133)

    def test_defaults_are_not_mutated_by_callers(self):
        settings = core.load_settings("/nonexistent/config.json")
        settings["wpm"] = 999
        self.assertNotEqual(core.DEFAULTS["wpm"], 999)


class ApplySettingTest(unittest.TestCase):
    def test_numeric_setting_is_stored(self):
        updated = core.apply_setting(core.DEFAULTS, "font_size", 52)
        self.assertEqual(updated["font_size"], 52)

    def test_numeric_setting_is_clamped_to_its_range(self):
        self.assertEqual(core.apply_setting(core.DEFAULTS, "font_size", 5000)["font_size"], 200)
        self.assertEqual(core.apply_setting(core.DEFAULTS, "font_size", 1)["font_size"], 16)

    def test_alpha_is_clamped_between_30_and_100_percent(self):
        self.assertEqual(core.apply_setting(core.DEFAULTS, "alpha", 0.0)["alpha"], 0.30)
        self.assertEqual(core.apply_setting(core.DEFAULTS, "alpha", 5.0)["alpha"], 1.0)

    def test_unknown_setting_is_rejected(self):
        with self.assertRaises(ValueError):
            core.apply_setting(core.DEFAULTS, "no_existe", 1)

    def test_non_numeric_value_for_a_numeric_setting_is_rejected(self):
        with self.assertRaises(ValueError):
            core.apply_setting(core.DEFAULTS, "font_size", "grande")

    def test_text_setting_rejects_empty_values(self):
        with self.assertRaises(ValueError):
            core.apply_setting(core.DEFAULTS, "font_family", "   ")

    def test_text_setting_is_trimmed(self):
        updated = core.apply_setting(core.DEFAULTS, "font_family", "  DejaVu Serif ")
        self.assertEqual(updated["font_family"], "DejaVu Serif")

    def test_apply_setting_does_not_mutate_the_input(self):
        original = dict(core.DEFAULTS)
        core.apply_setting(original, "wpm", 190)
        self.assertEqual(original, core.DEFAULTS)


class DefaultsCoverageTest(unittest.TestCase):
    def test_every_setting_the_menu_writes_has_a_default(self):
        for key in (
            "font_family",
            "font_size",
            "height",
            "alpha",
            "wpm",
            "read_head",
            "target_seconds",
            "show_read_head",
            "dim_spoken",
            "script",
            "monitor",
            "window_type",
            "fallback_x",
            "fallback_width",
        ):
            self.assertIn(key, core.DEFAULTS, f"falta el default de {key}")

    def test_every_numeric_limit_has_a_default_inside_its_range(self):
        for key, (low, high) in core.LIMITS.items():
            self.assertIn(key, core.DEFAULTS, f"falta el default de {key}")
            self.assertGreaterEqual(core.DEFAULTS[key], low)
            self.assertLessEqual(core.DEFAULTS[key], high)


class SectionsTest(unittest.TestCase):
    def test_script_without_headings_has_no_sections(self):
        self.assertEqual(core.parse_sections("solo texto\n\nmas texto"), [])

    def test_each_heading_becomes_a_section_with_its_paragraph_index(self):
        text = "intro\n\n# Bloque A\ntexto a\n\n# Bloque B\ntexto b"
        self.assertEqual(
            core.parse_sections(text),
            [
                {"title": "Bloque A", "paragraph": 1},
                {"title": "Bloque B", "paragraph": 2},
            ],
        )

    def test_heading_inside_a_paragraph_points_at_the_paragraph_being_read(self):
        self.assertEqual(core.parse_sections("uno\n# A\ndos"), [{"title": "A", "paragraph": 0}])

    def test_heading_marker_is_not_part_of_the_title(self):
        self.assertEqual(core.parse_sections("#    Cierre   ")[0]["title"], "Cierre")

    def test_notes_are_not_sections(self):
        self.assertEqual(core.parse_sections("> nota\ntexto"), [])


class PaceStatusTest(unittest.TestCase):
    def test_on_schedule_has_no_delta(self):
        status = core.pace_status(42, 42 / 85, 85)
        self.assertEqual(status["state"], "ok")
        self.assertAlmostEqual(status["remaining_s"], 43)
        self.assertAlmostEqual(status["delta_s"], 0)

    def test_behind_reports_the_remaining_time_and_a_late_state(self):
        status = core.pace_status(elapsed_s=30, progress_ratio=0.2, target_s=85)
        self.assertEqual(status["state"], "behind")
        self.assertAlmostEqual(status["remaining_s"], 68)
        self.assertAlmostEqual(status["delta_s"], 13)

    def test_ahead_reports_a_negative_delta(self):
        status = core.pace_status(elapsed_s=10, progress_ratio=0.4, target_s=85)
        self.assertEqual(status["state"], "ahead")
        self.assertAlmostEqual(status["delta_s"], -24)

    def test_a_tiny_deviation_stays_within_tolerance(self):
        status = core.pace_status(elapsed_s=20, progress_ratio=0.22, target_s=85)
        self.assertEqual(status["state"], "ok")

    def test_zero_target_is_safe(self):
        status = core.pace_status(0, 0, 0)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["remaining_s"], 0)


class DurationTest(unittest.TestCase):
    def test_estimated_duration_uses_the_current_pace(self):
        self.assertAlmostEqual(core.estimated_seconds(216, 152), 85.3, places=1)

    def test_estimated_duration_of_an_empty_script_is_zero(self):
        self.assertEqual(core.estimated_seconds(0, 152), 0.0)


if __name__ == "__main__":
    unittest.main()
