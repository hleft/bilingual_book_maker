import re
from copy import copy


class EPUBBookLoaderHelper:
    def __init__(self, translate_model, accumulated_num, translation_style):
        self.translate_model = translate_model
        self.accumulated_num = accumulated_num
        self.translation_style = translation_style

    def insert_with_style(self, p, text, translation_style=""):
        if p.string is not None and p.string.strip() == text.strip():
            return
        new_p = copy(p)
        new_p.string = text
        if translation_style != "":
            new_p["style"] = translation_style
        p.insert_after(new_p)

    def deal_new(self, p, wait_p_list):
        self.deal_old(wait_p_list)
        self.insert_with_style(
            p, self.translate_model.translate(p.text), self.translation_style
        )

    def deal_old(self, wait_p_list):
        if not wait_p_list:
            return

        result_txt_list = self.translate_model.translate_list(wait_p_list)

        for i in range(len(wait_p_list)):
            if i < len(result_txt_list):
                p = wait_p_list[i]
                self.insert_with_style(p, result_txt_list[i], self.translation_style)

        wait_p_list.clear()


def is_text_link(text):
    url_pattern = re.compile(
        r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
    )
    return bool(url_pattern.match(text.strip()))


def is_text_tail_link(text, num=100):
    text = text.strip()
    url_pattern = re.compile(
        r".*http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+$"
    )
    return bool(url_pattern.match(text)) and len(text) < num


def is_text_source(text):
    return text.strip().startswith("Source: ")


def is_text_list(text, num=80):
    text = text.strip()
    return re.match(r"^Listing\s*\d+", text) and len(text) < num


def is_text_figure(text, num=80):
    text = text.strip()
    return re.match(r"^Figure\s*\d+", text) and len(text) < num
