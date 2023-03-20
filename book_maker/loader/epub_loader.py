import os
import pickle
import string
import sys
from copy import copy
from pathlib import Path

from bs4 import BeautifulSoup as bs
from bs4.element import NavigableString
from ebooklib import ITEM_DOCUMENT, epub
from rich import print
from tqdm import tqdm

from book_maker.utils import num_tokens_from_text, prompt_config_to_kwargs

from .base_loader import BaseBookLoader
from .helper import EPUBBookLoaderHelper, not_trans, is_text_link


class EPUBBookLoader(BaseBookLoader):
    def __init__(
        self,
        epub_name,
        model,
        key,
        resume,
        language,
        model_api_base=None,
        is_test=False,
        test_num=5,
        prompt_config=None,
    ):
        self.epub_name = epub_name
        self.new_epub = epub.EpubBook()
        self.translate_model = model(
            key,
            language,
            api_base=model_api_base,
            **prompt_config_to_kwargs(prompt_config),
        )
        self.is_test = is_test
        self.test_num = test_num
        self.translate_tags = "p"
        self.allow_navigable_strings = False
        self.accumulated_num = 1
        self.translation_style = ""
        self.helper = EPUBBookLoaderHelper(
            self.translate_model, self.accumulated_num, self.translation_style
        )
        self.retranslate = None

        # monkey pathch for # 173
        def _write_items_patch(obj):
            for item in obj.book.get_items():
                if isinstance(item, epub.EpubNcx):
                    obj.out.writestr(
                        "%s/%s" % (obj.book.FOLDER_NAME, item.file_name), obj._get_ncx()
                    )
                elif isinstance(item, epub.EpubNav):
                    obj.out.writestr(
                        "%s/%s" % (obj.book.FOLDER_NAME, item.file_name),
                        obj._get_nav(item),
                    )
                elif item.manifest:
                    obj.out.writestr(
                        "%s/%s" % (obj.book.FOLDER_NAME, item.file_name), item.content
                    )
                else:
                    obj.out.writestr("%s" % item.file_name, item.content)

        epub.EpubWriter._write_items = _write_items_patch

        try:
            self.origin_book = epub.read_epub(self.epub_name)
        except Exception:
            # tricky monkey pathch for #71 if you don't know why please check the issue and ignore this
            # when upstream change will TODO fix this
            def _load_spine(obj):
                spine = obj.container.find("{%s}%s" % (epub.NAMESPACES["OPF"], "spine"))

                obj.book.spine = [
                    (t.get("idref"), t.get("linear", "yes")) for t in spine
                ]
                obj.book.set_direction(spine.get("page-progression-direction", None))

            epub.EpubReader._load_spine = _load_spine
            self.origin_book = epub.read_epub(self.epub_name)

        self.p_to_save = []
        self.resume = resume
        self.bin_path = f"{Path(epub_name).parent}/.{Path(epub_name).stem}.temp.bin"
        if self.resume:
            self.load_state()

    @staticmethod
    def _is_special_text(text):
        return (
            text.isdigit()
            or text.isspace()
            or is_text_link(text)
            or all(char in string.punctuation for char in text)
        )

    def _make_new_book(self, book):
        new_book = epub.EpubBook()
        new_book.metadata = book.metadata
        new_book.spine = book.spine
        new_book.toc = book.toc
        return new_book

    def _process_paragraph(self, p, index, p_to_save_len):
        if not p.text or self._is_special_text(p.text):
            return index

        new_p = copy(p)

        if self.resume and index < p_to_save_len:
            new_p.string = self.p_to_save[index]
        else:
            if type(p) == NavigableString:
                new_p = self.translate_model.translate(p.text)
                self.p_to_save.append(new_p)
            else:
                new_p.string = self.translate_model.translate(p.text)
                self.p_to_save.append(new_p.text)

        self.helper.insert_trans(p, new_p.string, self.translation_style)
        index += 1

        if index % 20 == 0:
            self._save_progress()

        return index

    def translate_paragraphs_acc(self, p_list, send_num):
        count = 0
        wait_p_list = []
        for i in range(len(p_list)):
            p = p_list[i]
            temp_p = copy(p)
            for sup in temp_p.find_all("sup"):
                sup.extract()
            if any(
                [not p.text, self._is_special_text(temp_p.text), not_trans(temp_p.text)]
            ):
                if i == len(p_list) - 1:
                    self.helper.deal_old(wait_p_list)
                continue
            length = num_tokens_from_text(temp_p.text)
            if length > send_num:
                self.helper.deal_new(p, wait_p_list)
                continue
            if i == len(p_list) - 1:
                if count + length < send_num:
                    wait_p_list.append(p)
                    self.helper.deal_old(wait_p_list)
                else:
                    self.helper.deal_new(p, wait_p_list)
                break
            if count + length < send_num:
                count += length
                wait_p_list.append(p)
                # This is because the more paragraphs, the easier it is possible to translate different numbers of paragraphs, maybe you should find better values than 15 and 2
                # if len(wait_p_list) > 15 and count > send_num / 2:
                #     self.helper.deal_old(wait_p_list)
                #     count = 0
            else:
                self.helper.deal_old(wait_p_list)
                wait_p_list.append(p)
                count = length

    def get_item(self, book, name):
        for item in book.get_items():
            if item.file_name == name:
                return item

    def get_index(self, p_list, fixstart, fixend):
        start_index = None
        end_index = None

        for index, p in enumerate(p_list):
            if fixstart in p.get_text():
                start_index = index
            if fixend in p.get_text():
                end_index = index

        return start_index, end_index

    def retranslate_book(self, index, p_to_save_len, pbar, trans_taglist, retranslate):
        complete_book_name = retranslate[0]
        fixname = retranslate[1]
        fixstart = retranslate[2]
        fixend = retranslate[3]

        if fixend == "":
            fixend = fixstart

        name, _ = os.path.splitext(self.epub_name)
        name_fix = f"{name}_fix.epub"

        complete_book = epub.read_epub(complete_book_name)
        new_book = self._make_new_book(complete_book)

        complete_item = self.get_item(complete_book, fixname)
        if complete_item is None:
            return

        ori_item = self.get_item(self.origin_book, fixname)
        if ori_item is None:
            return

        soup_complete = bs(complete_item.content, "html.parser")
        soup_ori = bs(ori_item.content, "html.parser")

        p_list_complete = soup_complete.findAll()
        p_list_ori = soup_ori.findAll()

        start_index, end_index = self.get_index(p_list_complete, fixstart, fixend)
        ori_start_index, ori_end_index = self.get_index(p_list_ori, fixstart, fixend)

        if ori_end_index == None:
            return

        p_list_complete_ori = p_list_ori[ori_start_index : ori_end_index + 1]

        if start_index == None or end_index == None:
            return

        # remove the middle translation
        i = 0
        for tag in p_list_complete:
            if i >= start_index and i <= end_index + 1:
                tag.extract()
            i = i + 1

        # Add the following original text
        i = 0
        for tag in p_list_complete:
            if i >= start_index - 1:
                target_tag = tag
                for t in p_list_complete_ori:
                    target_tag.insert_after(t)
                    target_tag = t
                break
            i = i + 1

        for item in complete_book.get_items():
            if item.file_name != fixname:
                new_book.add_item(item)

        complete_item.content = soup_complete.prettify().encode()

        # =================================================
        index = self.process_item(
            complete_item,
            index,
            p_to_save_len,
            pbar,
            new_book,
            trans_taglist,
            fixstart,
            fixend,
        )
        epub.write_epub(f"{name_fix}", new_book, {})

    def process_item(
        self,
        item,
        index,
        p_to_save_len,
        pbar,
        new_book,
        trans_taglist,
        fixstart=None,
        fixend=None,
    ):
        if not os.path.exists("log"):
            os.makedirs("log")

        soup = bs(item.content, "html.parser")
        p_list = soup.findAll(trans_taglist)

        if self.retranslate:
            new_p_list = []

            if fixstart is None or fixend is None:
                return

            start_append = False
            for p in p_list:
                text = p.get_text()
                if fixstart in text or fixend in text or start_append:
                    start_append = True
                    new_p_list.append(p)
                if fixend in text:
                    p_list = new_p_list
                    break

        if self.allow_navigable_strings:
            p_list.extend(soup.findAll(text=True))

        send_num = self.accumulated_num
        if send_num > 1:
            with open("log/buglog.txt", "a") as f:
                print(f"------------- {item.file_name} -------------", file=f)

            print("------------------------------------------------------")
            print(f"dealing {item.file_name} ...")
            self.translate_paragraphs_acc(p_list, send_num)
        else:
            is_test_done = self.is_test and index > self.test_num
            for p in p_list:
                if is_test_done:
                    break
                index = self._process_paragraph(p, index, p_to_save_len)
                # pbar.update(delta) not pbar.update(index)?
                pbar.update(1)
                if self.is_test and index >= self.test_num:
                    break

        item.content = soup.prettify().encode()
        new_book.add_item(item)

        return index

    def make_bilingual_book(self):
        self.helper = EPUBBookLoaderHelper(
            self.translate_model, self.accumulated_num, self.translation_style
        )
        new_book = self._make_new_book(self.origin_book)
        all_items = list(self.origin_book.get_items())
        trans_taglist = self.translate_tags.split(",")
        all_p_length = sum(
            0
            if i.get_type() != ITEM_DOCUMENT
            else len(bs(i.content, "html.parser").findAll(trans_taglist))
            for i in all_items
        )
        all_p_length += self.allow_navigable_strings * sum(
            0
            if i.get_type() != ITEM_DOCUMENT
            else len(bs(i.content, "html.parser").findAll(text=True))
            for i in all_items
        )
        pbar = tqdm(total=self.test_num) if self.is_test else tqdm(total=all_p_length)
        index = 0
        p_to_save_len = len(self.p_to_save)
        try:
            if self.retranslate:
                self.retranslate_book(
                    index, p_to_save_len, pbar, trans_taglist, self.retranslate
                )
                exit(0)
            # Add the things that don't need to be translated first, so that you can see the img after the interruption
            for item in self.origin_book.get_items():
                if item.get_type() != ITEM_DOCUMENT:
                    new_book.add_item(item)

            for item in self.origin_book.get_items_of_type(ITEM_DOCUMENT):
                # if item.file_name != "OEBPS/ch01.xhtml":
                #     continue
                index = self.process_item(
                    item, index, p_to_save_len, pbar, new_book, trans_taglist
                )

                if self.accumulated_num > 1:
                    name, _ = os.path.splitext(self.epub_name)
                    epub.write_epub(f"{name}_bilingual.epub", new_book, {})
            name, _ = os.path.splitext(self.epub_name)
            epub.write_epub(f"{name}_bilingual.epub", new_book, {})
            if self.accumulated_num == 1:
                pbar.close()
        except (KeyboardInterrupt, Exception) as e:
            print(e)
            if self.accumulated_num == 1:
                print("you can resume it next time")
                self._save_progress()
                self._save_temp_book()
            sys.exit(0)

    def load_state(self):
        try:
            with open(self.bin_path, "rb") as f:
                self.p_to_save = pickle.load(f)
        except Exception:
            raise Exception("can not load resume file")

    def _save_temp_book(self):
        # TODO refactor this logic
        origin_book_temp = epub.read_epub(self.epub_name)
        new_temp_book = self._make_new_book(origin_book_temp)
        p_to_save_len = len(self.p_to_save)
        trans_taglist = self.translate_tags.split(",")
        index = 0
        try:
            for item in origin_book_temp.get_items():
                if item.get_type() == ITEM_DOCUMENT:
                    soup = bs(item.content, "html.parser")
                    p_list = soup.findAll(trans_taglist)
                    if self.allow_navigable_strings:
                        p_list.extend(soup.findAll(text=True))
                    for p in p_list:
                        if not p.text or self._is_special_text(p.text):
                            continue
                        # TODO banch of p to translate then combine
                        # PR welcome here
                        if index < p_to_save_len:
                            new_p = copy(p)
                            if type(p) == NavigableString:
                                new_p = self.p_to_save[index]
                            else:
                                new_p.string = self.p_to_save[index]
                            self.helper.insert_trans(
                                p, new_p.string, self.translation_style
                            )
                            index += 1
                        else:
                            break
                    # for save temp book
                    item.content = soup.prettify().encode()
                new_temp_book.add_item(item)
            name, _ = os.path.splitext(self.epub_name)
            epub.write_epub(f"{name}_bilingual_temp.epub", new_temp_book, {})
        except Exception as e:
            # TODO handle it
            print(e)

    def _save_progress(self):
        try:
            with open(self.bin_path, "wb") as f:
                pickle.dump(self.p_to_save, f)
        except Exception:
            raise Exception("can not save resume file")
