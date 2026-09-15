# -*- coding: utf-8 -*-
"""验证试卷持久化 + 作答草稿：刷新不换卷、作答能恢复、只有重新组卷才换。"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8765"
OUT = Path(Path(__file__).resolve().parent.parent.parent / "source" / "_shots")


async def pid(page):
    return await page.evaluate("() => window.__PAPER_ID || null")


async def answered(page):
    return await page.evaluate("() => document.querySelectorAll('#singles input[type=radio]:checked').length")


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        page = await b.new_page(viewport={"width": 1180, "height": 1000})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

        print("① 首次打开")
        await page.goto(BASE + "/", wait_until="networkidle", timeout=180000)
        await page.wait_for_timeout(3000)
        p1 = await pid(page)
        print(f"   paper_id = {p1}")

        print("② 答 3 题")
        groups = {}
        for r in await page.query_selector_all("#singles input[type=radio]"):
            groups.setdefault(await r.get_attribute("name"), []).append(r)
        for nm, rs in list(groups.items())[:3]:
            await rs[0].check()
        await page.wait_for_timeout(500)
        print(f"   已答 {await answered(page)} 题")

        print("③ 刷新页面（模拟不小心关掉又打开）")
        await page.reload(wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        p2 = await pid(page)
        a2 = await answered(page)
        print(f"   paper_id = {p2}   {'✅ 同一份卷子' if p1 == p2 else '❌ 换卷了！'}")
        print(f"   恢复的作答 = {a2} 题   {'✅ 作答保住了' if a2 == 3 else '❌ 作答丢了'}")
        timer = await page.text_content("#timer")
        print(f"   计时器: {timer}")

        print("④ 点「重新组卷」")
        await page.click("text=重新组卷")
        await page.wait_for_timeout(6000)
        p3 = await pid(page)
        print(f"   paper_id = {p3}   {'✅ 已换新卷' if p3 != p2 else '❌ 没换'}")
        print(f"   新卷作答数 = {await answered(page)}")

        await page.screenshot(path=OUT / "P1_after_rebuild.png")
        print("\n前端错误:", errs[:5] if errs else "无 ✅")
        await b.close()


asyncio.run(main())
