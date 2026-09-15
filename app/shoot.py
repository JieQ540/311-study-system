# -*- coding: utf-8 -*-
"""用 Playwright 截图，亲眼检查页面渲染效果。

这一步是为了避免「数据看着对、界面一坨」——之前 56 题排版翻车就是这个原因。
"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from playwright.async_api import async_playwright

OUT = Path(Path(__file__).resolve().parent.parent / "source" / "_shots")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8765"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1180, "height": 1000})

        # 1) 模拟卷首页
        await page.goto(BASE + "/", wait_until="networkidle", timeout=180000)
        await page.wait_for_timeout(2500)
        await page.screenshot(path=OUT / "01_exam_top.png")
        print("已截图 01_exam_top.png")

        # 2) 往下滚，看主观题区域（含第 54 题材料）
        await page.evaluate("""() => {
            const hs = [...document.querySelectorAll('h2')];
            const t = hs.find(h => h.textContent.includes('主观题'));
            if (t) t.scrollIntoView();
        }""")
        await page.wait_for_timeout(800)
        await page.screenshot(path=OUT / "02_exam_subjective.png")
        print("已截图 02_exam_subjective.png")

        # 3) 日常练习页第一步
        await page.goto(BASE + "/daily", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(1200)
        await page.screenshot(path=OUT / "03_daily_step1.png")
        print("已截图 03_daily_step1.png")

        # 4) 点「看薄弱点」
        await page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(1500)
        try:
            await page.click("text=看薄弱点")
            await page.wait_for_timeout(2500)
            await page.screenshot(path=OUT / "04_weakness.png")
            print("已截图 04_weakness.png")
        except Exception as e:
            print("点击看薄弱点失败:", e)

        await browser.close()


asyncio.run(main())
