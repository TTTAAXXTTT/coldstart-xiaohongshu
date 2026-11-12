package main

import "testing"

func TestInventoryAbsenceRequiresCompleteSpecificEvidence(t *testing.T) {
	cases := []struct {
		name, text, title string
		ok                bool
	}{
		{"empty", "全部 0\n审核中\n未通过\n没有找到相关笔记", "新稿", true},
		{"existing_other_post", "全部 1\n已发布\n审核中\n未通过\n原有稿件\n2026-11-11 10:59\n31\n1", "新稿", true},
		{"original_already_submitted", "全部 1\n审核中\n未通过\n新稿\n2026-11-11 15:05", "新稿", false},
		{"partially_loaded", "全部 2\n审核中\n未通过\n原有稿件\n2026-11-11 10:59", "新稿", false},
		{"unknown_tab", "全部 1\n原有稿件\n2026-11-11 10:59", "新稿", false},
		{"empty_not_loaded", "全部 0\n审核中\n未通过", "新稿", false},
		{"pagination", "全部 1\n审核中\n未通过\n原稿\n2026-11-11 10:59\n下一页", "新稿", false},
		{"missing_title", "全部 0\n审核中\n未通过\n没有找到相关笔记", "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, ok := inventoryAbsence(c.text, c.title)
			if ok != c.ok {
				t.Fatalf("got %v want %v", ok, c.ok)
			}
		})
	}
}
