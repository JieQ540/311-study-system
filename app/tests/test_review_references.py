"""本场题号映射：纯逻辑回归，不读取学习库。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review

def main():
    questions = [
        {"question_id": 735, "attempt_id": 292, "stem": "留美幼童籍贯"},
        {"question_id": 736, "attempt_id": 293, "stem": "维新派学堂"},
        {"question_id": 1066, "attempt_id": 305, "stem": "维新派教育主张"},
        {"question_id": 1312, "attempt_id": None, "stem": "辨析题未作答"}]
    order = [1, 2, 3, 4, 5, 6, 7, 735, 736] + list(range(10, 21)) + [1066, 1312]
    refs = review.question_references(questions, order)
    cases = [
        ("作答ID对应当天第8题", refs["a292"]["label"] == "第 8 题"),
        ("作答ID对应当天第9题", refs["a293"]["label"] == "第 9 题"),
        ("主观题保留题单位置", refs["a305"]["label"] == "第 21 题"),
        ("未答题不因无作答ID丢失映射", refs["q1312"]["label"] == "第 22 题"),
        ("单次作答题库引用也回到本场作答", refs["q735"]["key"] == "a292"),
        ("不虚构跨场引用", "a291" not in refs),
    ]
    repeated = review.question_references([questions[0], {**questions[0], "attempt_id": 400}, questions[1]], [735, 736])
    cases += [
        ("重复作答不挤占下一题号", repeated["a293"]["label"] == "第 2 题"),
        ("重复作答身份分别保留", repeated["a400"]["label"] == "第 1 题（第 2 次作答）"),
        ("同题多次作答不猜q引用指向", "q735" not in repeated),
        ("缺原题单时明确记录顺序", "记录顺序" in review.question_references(questions)["a292"]["label"]),
        ("元数据不改写原题记录", "question_label" not in questions[0]),
    ]
    for name, passed in cases:
        print(("[PASS] " if passed else "[FAIL] ") + name)
    return int(not all(passed for _, passed in cases))

if __name__ == "__main__":
    sys.exit(main())
