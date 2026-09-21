import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_quiz as b


def rich(text, bold=False):
    return {"plain_text": text, "annotations": {"bold": bold}}


def block(kind, text="", ident="b", nested=False, tokens=None):
    return {"id": ident, "type": kind, "has_children": nested,
            kind: {"rich_text": tokens if tokens is not None else [rich(text)]}}


def particle(title, rows):
    """接續表形狀的文法點：每一列是（接續描述, 例）。"""
    return {"title": title, "rules": [list(r) for r in rows], "examples": []}


def entry(title, key, example):
    return {"title": title, "rules": [["名詞＋" + key, example]],
            "examples": [[example, "中文翻譯"]]}


class TextTests(unittest.TestCase):
    def test_quotes_are_not_split(self):
        text = '彼は「本当？ 大丈夫！」と言った。私は帰った。'
        self.assertEqual(b.sentence_with(text, '大丈夫'), '彼は「本当？ 大丈夫！」と言った。')
        self.assertEqual(b.sentence_with(text, '帰った'), '私は帰った。')

    def test_timestamp_range_and_punctuation(self):
        self.assertEqual(b.strip_ts('[00:00:03 –> 00:00:35] 本(ほん)です!'), '本（ほん）です！')
        self.assertEqual(b.normalize_text('4.5%です。'), '4.5%です。')
        self.assertEqual(b.strip_ts('(1:04) 本です。'), '本です。')

    def test_missing_punctuation_is_restored_before_extracting(self):
        self.assertEqual(b.sentence_with('ありますよね 私はああいうのが好きじゃなくって', 'ああいう'),
                         '私はああいうのが好きじゃなくって。')
        self.assertEqual(b.sentence_with('話したいときに使います なので役に立てばいいですね！', '立てばいい'),
                         'なので役に立てばいいですね！')
        self.assertEqual(b.sentence_with('楽しみにしててください😊 はいもう春ですね！', '春'), 'はいもう春ですね！')

    def test_punctuation_does_not_change_words_or_readings(self):
        source = '申請（しんせい）に 多分有利になったかな 結婚してる方が、証明しやすいですよね'
        restored = b.restore_punctuation(source)
        self.assertEqual(restored, '申請（しんせい）に多分有利になったかな。結婚してる方が、証明しやすいですよね。')
        clean = lambda text: b.re.sub(r'[\s。、！？]', '', text)
        self.assertEqual(clean(restored), clean(source))
        self.assertEqual(b.restore_punctuation(restored), restored)

    def test_questions_quotes_numbers_and_english_are_preserved(self):
        self.assertEqual(b.restore_punctuation('これは本ですか'), 'これは本ですか？')
        existing = '彼は「本当？ 大丈夫！」と言った。価格は4.5%上がった。'
        self.assertEqual(b.restore_punctuation(existing), existing)
        self.assertEqual(b.restore_punctuation('Bite Size Japaneseを聞きます'), 'Bite Size Japaneseを聞きます。')
        self.assertEqual(b.restore_punctuation('そう思います という話です'), 'そう思いますという話です。')
        self.assertEqual(b.restore_punctuation('結婚してる 方が多いです'), '結婚してる方が多いです。')
        self.assertEqual(b.restore_punctuation('知らない 人に会いました'), '知らない人に会いました。')

    def test_line_breaks_and_unspaced_sentence_starts(self):
        self.assertEqual(b.restore_punctuation('今日は休みです\n明日は学校に行きます'), '今日は休みです。明日は学校に行きます。')
        self.assertEqual(b.restore_punctuation('今日は休みです明日は学校に行きます'), '今日は休みです。明日は学校に行きます。')
        self.assertEqual(b.restore_punctuation('私は本を\n読みます'), '私は本を読みます。')
        self.assertEqual(b.restore_punctuation('お金がないと大変だし 問題もあるし そりゃ大切ですよね'),
                         'お金がないと大変だし、問題もあるし、そりゃ大切ですよね。')
        self.assertEqual(b.restore_punctuation('時代に合ってない もう少し考えたいです'), '時代に合ってない。もう少し考えたいです。')

    def test_punctuation_does_not_invent_missing_predicates(self):
        self.assertEqual(b.restore_punctuation('プレートが重なる場所に'), 'プレートが重なる場所に')
        self.assertEqual(b.sentence_with('プレートが重なる場所に', '重なる'), '')

    def test_marked_phrase_is_not_split(self):
        phrase = '今日は休みです 明日は学校に行きます'
        self.assertEqual(b.restore_punctuation(phrase, [phrase]), phrase + '。')

    def test_reading_is_removed_with_answer(self):
        self.assertEqual(b.blank_sentence('申請（しんせい）をします。', ['申請']),
                         ('［　］をします。', '申請（しんせい）'))
        self.assertEqual(b.blank_sentence('庭（にわ）に猫がいる。', ['に'])[0], '庭（にわ）［　］猫がいる。')

    def test_repeated_or_broken_blank_is_rejected(self):
        self.assertIsNone(b.blank_sentence('学校に行く前に食べる。', ['に']))
        self.assertIsNone(b.blank_sentence('「学校に行く。', ['学校']))
        self.assertIsNone(b.blank_sentence('「学校」。', ['学校']))
        self.assertIsNone(b.blank_sentence('学校に行く。', ['missing']))

    def test_reading_variants_are_same_option(self):
        self.assertEqual(b.unique_options(['本（ほん）', '本(ほん)', '猫', '犬'], '本'), ['猫', '犬'])

    def test_marked_phrases_and_grammar_particles(self):
        phrase = '友達と一緒に美しい景色を眺めながらゆっくりと長い道を歩いている'
        self.assertEqual(b.marked_terms([rich(phrase, True)]), [phrase])
        self.assertEqual(b.marked_terms([rich('に', True)], grammar=True), ['に'])
        self.assertEqual(b.marked_terms([rich('〜ように', True)]), [])


class ParserTests(unittest.TestCase):
    def test_repaired_vocab_keeps_the_exact_original_paragraph(self):
        raw = '[0:43] 子猫(こねこ)を見ました 次は公園に行きます'
        tree = {'root': [block('paragraph', tokens=[rich('[0:43] '), rich('子猫(こねこ)', True),
                                                   rich('を見ました 次は公園に行きます')])]}
        with patch.object(b, 'children', side_effect=lambda key: tree[key]):
            items = b.collect_vocab('root', [])
        self.assertEqual(items[0]['sentence'], '子猫（こねこ）を見ました。')
        self.assertEqual(items[0]['source_text'], raw)
        self.assertTrue(items[0]['punctuation_restored'])

    def test_nested_notes_and_immediate_translation(self):
        tree = {
            'root': [{'type': 'child_page', 'id': 'article', 'child_page': {'title': '記事'}},
                     {'type': 'child_page', 'id': 'grammar', 'child_page': {'title': '文法'}}],
            'article': [block('toggle', '逐字稿', 'toggle', True)],
            'toggle': [block('bulleted_list_item', ident='jp', tokens=[rich('猫', True), rich('を見ました。')]),
                       block('quote', '📝 看到了貓。'),
                       block('paragraph', tokens=[rich('用語', True), rich('：這是中文說明')]),
                       block('paragraph', tokens=[rich('補充說明', True), rich('：這是說明')])],
        }
        with patch.object(b, 'children', side_effect=lambda key: tree[key]):
            items = b.collect_vocab('root', ['grammar'])
        # 單字「猫」過短；改用詞彙級別的標記後才出題。
        self.assertEqual(items, [])
        tree['toggle'][0]['bulleted_list_item']['rich_text'][0] = rich('子猫', True)
        with patch.object(b, 'children', side_effect=lambda key: tree[key]):
            items = b.collect_vocab('root', ['grammar'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['hint'], '看到了貓。')
        self.assertEqual(items[0]['source'], '記事')

    def test_translation_does_not_cross_another_sentence(self):
        tree = {'root': [block('paragraph', tokens=[rich('子猫', True), rich('を見ました。')]),
                         block('paragraph', '犬がいました。'), block('quote', '📝 有隻狗。')]}
        with patch.object(b, 'children', side_effect=lambda key: tree[key]):
            self.assertEqual(b.collect_vocab('root', [])[0]['hint'], '')

    def test_only_rules_section_tables_and_nested_examples(self):
        row = {'type': 'table_row', 'table_row': {'cells': [[rich('名詞＋に')], [rich('学校に行く')]]}}
        table = {'type': 'table', 'id': 'table', 'table': {'has_column_header': False}}
        tree = {'root': [block('heading_2', '【格助詞　に】'), block('heading_3', '問題解析'), table,
                         block('heading_3', '接續方式'), table, block('heading_3', '例句'),
                         block('toggle', '', 'examples', True), block('heading_3', '補充說明'),
                         block('paragraph', 'これは例句ではありません。')],
                'table': [row], 'examples': [block('paragraph', tokens=[rich('学校'), rich('に', True), rich('行く。')]),
                                            block('quote', '📝 去學校。')]}
        with patch.object(b, 'children', side_effect=lambda key: tree[key]):
            grammar = b.collect_grammar('root')
        self.assertEqual(grammar[0]['rules'], [['名詞＋に', '学校に行く']])
        self.assertEqual(grammar[0]['examples'], [['学校に行く。', '去學校。']])
        self.assertEqual(grammar[0]['targets']['学校に行く。'], ['に'])

    def test_pagination(self):
        pages = [{'results': [1], 'has_more': True, 'next_cursor': 'next'},
                 {'results': [2], 'has_more': False}]
        with patch.object(b, 'api_get', side_effect=pages) as get:
            self.assertEqual(b.children('a-b'), [1, 2])
        self.assertIn('start_cursor=next', get.call_args.args[0])


class QuestionTests(unittest.TestCase):
    def vocab(self):
        return [{'term': term, 'sentence': sentence, 'source': '記事', 'hint': '翻譯'}
                for term, sentence in [('新聞', '毎朝、新聞を読みます。'), ('雑誌', 'この雑誌を買いました。'),
                                       ('辞書', '知らない言葉を辞書で調べます。'), ('切手', '封筒に切手を貼ります。')]]

    def test_rebuild_follows_additions_edits_and_deletions(self):
        original = self.vocab()
        first = b.build_questions(original, [])
        self.assertEqual(len(first), 4)
        changed = original[:3] + [{'term': '地図', 'sentence': '地図を買いました。', 'source': '新記事', 'hint': ''}]
        second = b.build_questions(changed, [])
        self.assertEqual({q['answer'] for q in second}, {'新聞', '雑誌', '辞書', '地図'})
        self.assertEqual(first[0]['id'], second[0]['id'])
        self.assertNotIn('切手', json.dumps(second, ensure_ascii=False))
        self.assertEqual(b.build_questions(changed[:3], []), [])
        self.assertTrue(all(q['hint'] == '' and q['translation'] == '翻譯' for q in first))

    def test_duplicate_questions_and_different_forms(self):
        vocab = self.vocab()
        self.assertEqual(len(b.build_questions(vocab + vocab, [])), 4)
        vocab[-1]['term'] = '走ります'
        self.assertEqual(b.build_questions(vocab, []), [])

    def test_sufficient_same_article_options_do_not_mix_other_topics(self):
        vocab = self.vocab() + [{'term': '子犬', 'sentence': '子犬を見かけました。', 'source': '別の記事', 'hint': ''}]
        qs = b.build_questions(vocab, [])
        self.assertTrue(all('子犬' not in q['pool'] for q in qs if q['source'] == '記事'))

    def test_conflicting_grammar_stems_are_omitted(self):
        grammar = [entry('格助詞　' + k, k, text) for k, text in
                   [('と', '財布とスマホを持つ。'), ('や', '財布やスマホを持つ。'),
                    ('に', '学校に行く。'), ('で', '学校で学ぶ。'), ('を', '学校を出る。'), ('が', '学校がある。')]]
        qs = b.build_questions([], grammar)
        self.assertFalse(any(q['kind'] == 'grammar' and '財布' in q['stem'] for q in qs))

    def test_usage_role_only_from_semantic_labels(self):
        self.assertEqual(b.usage_role('場所（ばしょ）＋に＋ある／いる'), '場所')
        self.assertEqual(b.usage_role('名詞（めいし）（起点・きてん）＋を＋移動動詞'), '起点')
        self.assertEqual(b.usage_role('名詞＋と＋名詞（全部列挙・ぜんぶれっきょ）'), '全部列挙')
        self.assertEqual(b.usage_role('職業名詞＋をしている（現在）'), '現在')
        # 詞類描述的是接續形式，不是語意；拿來當選項無法辨別用法
        self.assertIsNone(b.usage_role('な形容詞（けいようし）語幹＋で'))
        self.assertIsNone(b.usage_role('動詞普通形＋し'))
        # 帶著助詞的是句型描述，不是角色
        self.assertIsNone(b.usage_role('場所に＋名詞＋が＋いる／ある'))
        # 括號標了對照、別用法的不是這個文法點的用法
        self.assertIsNone(b.usage_role('（対比）意志動詞辞書形＋ために'))

    def test_usage_question_asks_about_the_particle(self):
        grammar = [particle('格助詞　に', [
            ['場所（ばしょ）＋に＋ある／いる', '机の上に本がある'],
            ['時刻（じこく）＋に＋動詞', '7時に起きる'],
            ['目的地（もくてきち）＋に＋行く', '学校に行く'],
            ['結果（けっか）＋に＋なる', '医者になる']])]
        qs = [q for q in b.build_questions([], grammar) if q['kind'] == 'connect']
        self.assertEqual(len(qs), 4)
        # 題幹是例句本身，不洩漏文法點名稱
        self.assertIn('机の上に本がある', [q['stem'] for q in qs])
        self.assertTrue(all('格助詞' not in q['stem'] for q in qs))
        q = next(q for q in qs if q['stem'] == '机の上に本がある')
        self.assertIn('「に」', q['instruction'])
        self.assertEqual(q['answer'], '場所')
        # 同一個助詞的其他用法才是會混淆的誘答，夠用時不向外借
        self.assertEqual(set(q['pool']), {'時刻', '目的地', '結果'})

    def test_usage_distractors_exclude_roles_the_title_already_claims(self):
        grammar = [
            particle('格助詞　から（起点・きてん／口語）', [
                ['時間（じかん）／場所（ばしょ）＋から', '9時から始まる'],
                ['理由（りゆう）＋から', '疲れたから休む'],
                ['材料（ざいりょう）＋から＋作られる', '米から造られる']]),
            particle('格助詞　を', [
                ['名詞（めいし）（起点・きてん）＋を＋移動動詞', '電車を降りる'],
                ['名詞（めいし）（経過点・けいかてん）＋を＋移動動詞', '橋を渡る'],
                ['名詞（めいし）（対象・たいしょう）＋を＋他動詞', 'コーヒーを飲む']]),
        ]
        qs = [q for q in b.build_questions([], grammar) if q['kind'] == 'connect']
        kara = next(q for q in qs if q['stem'] == '9時から始まる')
        # 標題已寫明 から 是起点，「9時から」確實是起点，不能當錯誤選項
        self.assertNotIn('起点', kara['pool'])
        self.assertTrue({'理由', '材料'} <= set(kara['pool']))

    def test_inflection_pair_questions_need_no_tokenizer(self):
        grammar = [particle('な形容詞・名詞の中止形「〜で」', [
            ['な形容詞（けいようし）語幹＋で', '静（しず）かだ → 静（しず）かで'],
            ['い形容詞（けいようし）＋くて', '高（たか）い → 高（たか）くて'],
            ['動詞（どうし）て形（けい）', '食（た）べる → 食（た）べて']])]
        qs = [q for q in b.build_questions([], grammar) if q['label'] == '活用']
        self.assertEqual(len(qs), 3)
        q = next(q for q in qs if q['answer'] == '静（しず）かで')
        # 誘答是把別列的語尾套錯在這個詞上，加上忘了變形的原形
        self.assertIn('静（しず）かくて', q['pool'])
        self.assertIn('静（しず）かて', q['pool'])
        self.assertIn('静（しず）かだ', q['pool'])

    def test_furigana_prefix_keeps_the_reading_with_its_kanji(self):
        self.assertEqual(b.furigana_prefix('使（つか）う', 1), '使（つか）')
        self.assertEqual(b.furigana_prefix('話（はな）せる', 2), '話（はな）せ')
        self.assertEqual(b.furigana_prefix('食（た）べる', 0), '')

    @unittest.skipUnless(b.TAGGER, '未安裝斷詞器')
    def test_conjugation_uses_the_dictionary_not_a_guess(self):
        # る 結尾分不出五段／一段，猜錯會生出「見らない」這種不存在的詞
        self.assertEqual(b.verb_forms('切る', '五段-ラ行')['ない形'], '切らない')
        self.assertEqual(b.verb_forms('見る', '上一段-マ行')['ない形'], '見ない')
        self.assertEqual(b.verb_forms('行く', '五段-カ行')['て形'], '行って')   # 音便例外
        self.assertEqual(b.verb_forms('泳ぐ', '五段-ガ行')['た形'], '泳いだ')
        self.assertEqual(b.verb_forms('勉強する', 'サ行変格')['て形'], '勉強して')
        self.assertEqual(b.verb_forms('来る', 'カ行変格')['た形'], '来た')
        self.assertEqual(b.verb_forms('食べる', '未知の活用'), {})

    @unittest.skipUnless(b.TAGGER, '未安裝斷詞器')
    def test_verb_chunk_keeps_its_own_kanji_and_compound(self):
        # lemma 會把「帰る」正規化成「返る」，orthBase 才是這個詞自己的辞書形
        self.assertEqual(b.verb_chunks('帰る')[0][3], '帰る')
        # 可能形的 lemma 是「話す」，配上下一段活用型會生出「話て」
        self.assertEqual(b.verb_chunks('話せるようになる')[0][3], '話せる')
        # 名詞＋する 是一個複合動詞
        self.assertIn('メモする', [c[3] for c in b.verb_chunks('忘れないようにメモする')])

    @unittest.skipUnless(b.TAGGER, '未安裝斷詞器')
    def test_conjugation_target_follows_the_connection_column(self):
        self.assertEqual(b.conjugation_target('動詞辞書形＋名詞', 'よく使う言葉')[0], '使う')
        self.assertEqual(b.conjugation_target('動詞た形＋名詞', '昨日使った言葉')[0], '使った')
        # 例句有三個動詞，「と」限定了是哪一個
        self.assertEqual(
            b.conjugation_target('動詞ない形＋と＋いけない', '送ってもらわないといけない')[0],
            'もらわない')
        # 接續欄描述的不是動詞形就不出活用題
        self.assertIsNone(b.conjugation_target('名詞＋を＋他動詞', 'コーヒーを飲む'))

    @unittest.skipUnless(b.TAGGER, '未安裝斷詞器')
    def test_conjugation_blank_carries_furigana_into_options(self):
        grammar = [particle('動詞辞書形＋名詞', [
            ['動詞（どうし）辞書形（じしょけい）＋名詞（めいし）', 'よく使（つか）う言葉（ことば）']])]
        q = next(q for q in b.build_questions([], grammar) if q['label'] == '活用')
        self.assertEqual(q['stem'], 'よく［　］言葉（ことば）')
        self.assertEqual(q['answer'], '使（つか）う')
        self.assertIn('使（つか）った', q['pool'])
        self.assertIn('使（つか）い', q['pool'])
        # 肯定的正解不與否定形同組：「よく使わない言葉」也是對的
        self.assertNotIn('使（つか）わない', q['pool'])

    @unittest.skipUnless(b.TAGGER, '未安裝斷詞器')
    def test_conjugation_never_pairs_negative_against_affirmative(self):
        # 「よく使う言葉」與「よく使わない言葉」都成立，只是語意相反；
        # 作答時看不到文法點，沒有線索排除，所以這題整個不出。
        grammar = [particle('動詞辞書形＋名詞', [
            ['動詞（どうし）辞書形（じしょけい）＋名詞（めいし）', 'よく使（つか）う言葉（ことば）']])]
        qs = [q for q in b.build_questions([], grammar) if q['label'] == '活用']
        self.assertTrue(all('使（つか）わない' not in q['pool'] for q in qs))

    @unittest.skipUnless(b.TAGGER, '未安裝斷詞器')
    def test_conjugation_skips_plain_forms_before_youni(self):
        # 「忘れないように」是為了不忘，「忘れたように」是裝作忘了：都合法，
        # 只是語意不同。活用題不顯示文法點，沒有線索排除，所以這題整個不出。
        grammar = [particle('〜ように（目標となる状態）', [
            ['動詞（どうし）ない形（けい）＋ように＋動詞（どうし）', '忘（わす）れないようにメモする']])]
        self.assertEqual([q for q in b.build_questions([], grammar) if q['label'] == '活用'], [])

    @unittest.skipUnless(b.TAGGER, '未安裝斷詞器')
    def test_conjugation_never_puts_a_form_against_its_opposite(self):
        # 「送ってもらうといけない」也成立，只是語意相反。活用題不顯示文法點，
        # 學習者沒有線索排除，所以肯定與否定不放進同一組選項。
        grammar = [particle('ないといけない（義務・必須表現）', [
            ['動詞（どうし）ない形（けい）＋と＋いけない', '送（おく）ってもらわないといけない']])]
        q = next(q for q in b.build_questions([], grammar) if q['label'] == '活用')
        self.assertEqual(q['answer'], 'もらわない')
        self.assertNotIn('もらう', q['pool'])
        # 對立的只有辞書形，其他形仍然是有效誘答
        self.assertIn('もらって', q['pool'])
        self.assertIn('もらった', q['pool'])

    @unittest.skipUnless(b.TAGGER, '未安裝斷詞器')
    def test_conjugation_never_offers_a_form_the_note_also_accepts(self):
        # 筆記把辞書形與た形列在同一則，等於說兩種都能接名詞：
        # 「よく使った言葉」是對的，不能拿來當錯誤選項。
        grammar = [particle('動詞辞書形＋名詞', [
            ['動詞（どうし）辞書形（じしょけい）＋名詞（めいし）', 'よく使（つか）う言葉（ことば）'],
            ['動詞（どうし）た形（けい）＋名詞（めいし）', '昨日（きのう）使（つか）った言葉（ことば）']])]
        qs = [q for q in b.build_questions([], grammar) if q['label'] == '活用']
        # 辞書形那題的誘答被兩條規則削光：た形是筆記也接受的形，ない形與正解對立，
        # 剩下的不足三個，整題捨棄。留下的一題正解是た形。
        self.assertEqual([q['answer'] for q in qs], ['使（つか）った'])
        self.assertNotIn('使（つか）う', qs[0]['pool'])
        self.assertIn('使（つか）って', qs[0]['pool'])

    def test_usage_skips_examples_that_match_more_than_one_role(self):
        grammar = [
            particle('格助詞　に', [
                ['場所（ばしょ）＋に＋ある', '同じ例'],
                ['時刻（じこく）＋に＋動詞', '7時に起きる'],
                ['目的地（もくてきち）＋に＋行く', '学校に行く'],
                ['結果（けっか）＋に＋なる', '医者になる']]),
            particle('格助詞　で', [
                ['手段（しゅだん）＋で', '同じ例'],
                ['原因（げんいん）＋で', '台風で止まる'],
                ['期限（きげん）＋で＋終わる', '1時間で終わる']]),
        ]
        qs = [q for q in b.build_questions([], grammar) if q['kind'] == 'connect']
        self.assertNotIn('同じ例', [q['stem'] for q in qs])

    def test_failed_build_preserves_existing_bank(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'quiz.json'
            out.write_text('previous', encoding='utf-8')
            with patch.multiple(b, TOKEN='test', ARTICLES_ID='articles', GRAMMAR_ID='grammar', OUT=str(out)), \
                 patch.object(b, 'collect_vocab', return_value=[]), patch.object(b, 'collect_grammar', return_value=[]):
                with self.assertRaises(SystemExit):
                    b.main()
            self.assertEqual(out.read_text(encoding='utf-8'), 'previous')


if __name__ == '__main__':
    unittest.main()
