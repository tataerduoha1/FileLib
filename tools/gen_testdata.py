"""
tools/gen_testdata.py —— 合成测试资料库生成器（供自动化测试与性能基准用）

生成一个结构仿真的中文资料库：
  【小说】/<作者>/书名 - 作者.epub   真实 EPUB（含封面、中文正文，可全文检索）
  【小说】/<作者>/书名 - 作者.txt    真实中文长文
  【技术】/深度学习NN.pdf           真实 PDF（PyMuPDF 生成，含正文）
  【技术】/教程NN.md
  【视频】/<系列名>/系列名 S01E01.mp4  带 ftyp 头的伪视频（可测 Range 流式）
  【图片】/<相册NN>/img_NNNN.jpg    PIL 生成的渐变图（可测缩略图）
  【重复】/…                       与上面部分文件字节级一致（测去重）

用法：
  python tools/gen_testdata.py --root D:\\fltest --files 5000
  python tools/gen_testdata.py --root D:\\flbig  --files 120000 --seed 7
"""
from __future__ import annotations

import argparse
import io
import os
import random
import struct
import sys
import time
import zipfile
from pathlib import Path

AUTHORS = ["王小波", "刘慈欣", "金庸", "古龙", "东野圭吾", "村上春树",
           "余华", "莫言", "路遥", "阿来", "冯唐", "双雪涛",
           "紫金陈", "马伯庸", "江南", "沧月", "猫腻", "烽火戏诸侯"]

BOOKS = ["黄金时代", "三体", "球状闪电", "流浪地球", "射雕英雄传", "多情剑客无情剑",
         "白夜行", "解忧杂货店", "挪威的森林", "活着", "许三观卖血记", "丰乳肥臀",
         "平凡的世界", "尘埃落定", "万物生长", "平原上的摩西", "坏小孩", "长安十二时辰",
         "此间的少年", "听雪楼", "庆余年", "雪中悍刀行", "遥远的救世主", "天幕红尘"]

TOPICS = ["科幻", "武侠", "推理", "都市", "历史", "玄幻", "现实", "悬疑"]
SERIES = ["星际档案", "大唐风云", "神探手记", "城市之光", "荒原纪事", "深海行动"]
CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安"]

PARA = ("夜里下过一场雨，街道被洗得发亮。{}沿着河边走，看见灯火在水面上碎成一片一片。"
        "他想起很多年前也是这样的晚上，有人对他说过一句再普通不过的话，他却记到了现在。"
        "城市总是这样，把所有故事都埋进霓虹里，等某个失眠的人把它们重新挖出来。"
        "风从楼宇之间穿过去，带来一点凉意，也带来远处火车的汽笛声。{}")

SENT = ("第{}章　灯火与河", "他数着路灯往前走，一段亮，一段暗。",
        "水面的倒影被风揉皱，又慢慢平复。", "远处有人吹口琴，断断续续的调子。",
        "他想，也许所有的相遇都是久别重逢。", "雨后的空气里有泥土和青草的味道。")


def chinese_text(seed: int, paras: int = 40) -> str:
    r = random.Random(seed)
    out = []
    for i in range(paras):
        s1 = r.choice(SENT[1:])
        s2 = r.choice(SENT[1:])
        out.append(PARA.format(s1, s2) + f"（{r.choice(CITIES)}·{r.choice(TOPICS)}·段{i}）")
    return "\n\n".join(out)


def make_jpg(w: int = 480, h: int = 320, seed: int = 0) -> bytes:
    from PIL import Image, ImageDraw
    r = random.Random(seed)
    im = Image.new("RGB", (w, h))
    dr = ImageDraw.Draw(im)
    c1 = (r.randint(0, 255), r.randint(0, 255), r.randint(0, 255))
    c2 = (r.randint(0, 255), r.randint(0, 255), r.randint(0, 255))
    for y in range(h):
        t = y / max(1, h - 1)
        dr.line([(0, y), (w, y)],
                fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    for _ in range(12):
        x, y = r.randint(0, w), r.randint(0, h)
        dr.ellipse([x, y, x + r.randint(8, 60), y + r.randint(8, 60)],
                   outline=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86)
    return buf.getvalue()


def make_epub(title: str, author: str, seed: int) -> bytes:
    """最小可用 EPUB：mimetype + container + opf + 封面 + 若干章。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        cover = make_jpg(600, 900, seed)
        z.writestr("OEBPS/cover.jpg", cover)
        z.writestr("OEBPS/cover.xhtml",
                   '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>cover</title>'
                   '</head><body><img src="cover.jpg" alt="cover"/></body></html>')
        chapters = []
        nch = random.Random(seed).randint(3, 8)
        for i in range(1, nch + 1):
            h, body = SENT[0].format(i), chinese_text(seed + i, paras=25)
            chapters.append((f"ch{i}.xhtml", h, body))
        manifest = ('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                    '<item id="cover-image" href="cover.jpg" media-type="image/jpeg" '
                    'properties="cover-image"/>'
                    '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        manifest += "".join(
            f'<item id="c{i}" href="{fn}" media-type="application/xhtml+xml"/>'
            for i, (fn, _h, _b) in enumerate(chapters, 1))
        spine = "".join(f'<itemref idref="c{i}"/>' for i, _ in enumerate(chapters, 1))
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0" encoding="utf-8"?>'
                   '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
                   f'unique-identifier="bid"><metadata '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                   'xmlns:opf="http://www.idpf.org/2007/opf">'
                   f'<dc:title>{title}</dc:title>'
                   f'<dc:creator opf:role="aut">{author}</dc:creator>'
                   '<dc:language>zh</dc:language>'
                   '<dc:publisher>合成出版社</dc:publisher>'
                   f'<dc:description>《{title}》是{author}的一部测试用合成图书，'
                   f'主题涵盖{random.Random(seed).choice(TOPICS)}。</dc:description>'
                   '</metadata><manifest>' + manifest + '</manifest><spine toc="ncx">'
                   + spine + '</spine></package>')
        nav = "".join(
            f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{h}</text></navLabel>'
            f'<content src="{fn}"/></navPoint>'
            for i, (fn, h, _b) in enumerate(chapters, 1))
        z.writestr("OEBPS/toc.ncx",
                   '<?xml version="1.0" encoding="utf-8"?>'
                   '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                   f'<head/><docTitle><text>{title}</text></docTitle><navMap>' + nav +
                   '</navMap></ncx>')
        for fn, h, body in chapters:
            z.writestr(f"OEBPS/{fn}",
                       f'<?xml version="1.0" encoding="utf-8"?><html '
                       'xmlns="http://www.w3.org/1999/xhtml"><head><title>' + h +
                       '</title></head><body><h2>' + h + '</h2>' +
                       "".join(f"<p>{p}</p>" for p in body.split("\n\n")) +
                       '</body></html>')
    return buf.getvalue()


def make_pdf(title: str, author: str, seed: int) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    r = random.Random(seed)
    for p in range(r.randint(3, 10)):
        page = doc.new_page()
        try:
            page.insert_text((72, 90), f"{title}", fontsize=22, fontname="china-s")
            page.insert_text((72, 120), f"{author} · 合成测试文档 #{seed}", fontsize=11,
                             fontname="china-s")
            y = 150
            for i in range(28):
                line = f"{p+1}-{i}: " + chinese_text(seed * 31 + p, 1)[:46]
                page.insert_text((72, y), line, fontsize=10, fontname="china-s")
                y += 16
                if y > 780:
                    break
        except Exception:
            page.insert_text((72, 90), f"{title} (ascii)", fontsize=20)
    data = doc.tobytes()
    doc.close()
    return data


def make_mp4(size_kb: int, seed: int) -> bytes:
    """带 ftyp box 头的伪视频文件：足以支撑 HTTP Range 流式测试。"""
    r = random.Random(seed)
    head = (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
            b"\x00\x00\x00\x08free")
    body = bytes(r.getrandbits(8) for _ in range(1024))
    total = max(len(head), size_kb * 1024)
    out = bytearray(head)
    while len(out) < total:
        out += body
    return bytes(out[:total])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--files", type=int, default=5000, help="目标文件总数（约）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dup-every", type=int, default=20,
                    help="每 N 个文件放一个字节级重复副本（0=关闭）")
    ap.add_argument("--video-cap", type=int, default=0,
                    help="限制单个视频最大 KB（0=不限制；大库基准时控制总体积）")
    ap.add_argument("--clean", action="store_true", help="先清空 root")
    a = ap.parse_args()

    root = Path(a.root)
    if a.clean and root.exists():
        import shutil
        print(f"清空 {root} …")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    r = random.Random(a.seed)
    t0 = time.time()
    n = 0
    dup_q: list[tuple[bytes, str]] = []

    # 每个单元实际产出 ~13 个文件（1 epub + 1 txt + 1 pdf + 1 md + 2~5 mp4 + 3~9 jpg），
    # 单元数直接给到目标值即可——循环内 n >= files 时会精确截断。
    units = max(1, a.files)
    print(f"目标 ~{a.files:,} 文件，计划 {units} 个单元")

    def put(sub: str, name: str, data: bytes, note_dup: bool = True):
        nonlocal n
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        n += 1
        if note_dup and a.dup_every and n % a.dup_every == 0:
            dup_q.append((data, f"【重复】/{sub.strip('/')}__{name}"))

    for u in range(units):
        author = AUTHORS[u % len(AUTHORS)] + str(u // len(AUTHORS) or "")
        book = BOOKS[u % len(BOOKS)] + f"·{u:04d}"
        topic = TOPICS[u % len(TOPICS)]
        # 小说：EPUB + TXT
        put(f"【小说】/{topic}/{author}", f"{book} - {author}.epub",
            make_epub(book, author, a.seed + u))
        put(f"【小说】/{topic}/{author}", f"{book} - {author}.txt",
            chinese_text(a.seed + u, paras=60).encode("utf-8"))
        # 技术：PDF + MD
        put("【技术】/深度学习", f"深度学习讲义{u:04d} - {author}.pdf",
            make_pdf(f"深度学习讲义{u:04d}", author, a.seed + u))
        put("【技术】/编程", f"教程{u:04d}.md",
            (f"# 教程 {u}\n\n" + chinese_text(a.seed + u, 20)).encode("utf-8"))
        # 视频：系列 + 单集（2~90MB 拉开大小差距）
        series = SERIES[u % len(SERIES)]
        for ep in range(1, r.randint(2, 5)):
            kb = r.choice([300, 1200, 5200, 15000, 48000, 62000])
            if a.video_cap:
                kb = min(kb, a.video_cap)
            put(f"【视频】/{series}/S{u % 3 + 1:02d}",
                f"{series} S{u % 3 + 1:02d}E{ep:02d}.mp4",
                make_mp4(kb, a.seed + u * 100 + ep))
        # 图片：相册
        alb = f"相册{u % 17:02d}"
        for k in range(r.randint(3, 9)):
            put(f"【图片】/{alb}", f"IMG_{u:04d}_{k:03d}.jpg",
                make_jpg(seed=a.seed + u * 37 + k))
        if n >= a.files:
            break

    # 重复副本（同字节，不同路径 → 供三层去重识别）
    for data, rel in dup_q:
        d = root / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(data)
        n += 1

    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    print(f"完成：{n:,} 个文件，{size/1e6:.1f} MB，耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
