"""Traditional to Simplified Chinese for the books typeset in Taiwan.

《旋元佑英文阅读通》 and 《旋元佑GRE字汇通》 are only available as the
publisher's PDFs, in Traditional characters, while every other book of the
series is kept in Simplified.  :func:`to_simplified` converts their text
without a third-party dependency (the program also runs on Windows).

The tables were generated once with ICU's ``Hant-Hans`` transform (macOS
``NSString.stringByApplyingTransform``) over every Han character of the two
PDFs, and checked against ICU's phrase-aware conversion of the full text:
the phrases where a character converts differently from its default are in
``PHRASES``.  A character missing from the tables is kept as it is, so text
outside these books is never damaged, only perhaps left Traditional.
"""

from __future__ import annotations

import re

_TRADITIONAL = (
    "丟並乾亂亞佈佔併來侖侶係俱倉個們倖倫偉側偵偽傑傘備傢傭傳債傷傾僅僕僥僱價儀億儉儕"
    "儘償優儲儸兌兒內兩冊凍別刪則剋剛剝創劃劇劑勁動務勝勞勢勵勸匯區協卻厭厲參叢員唸問"
    "啟喚喪喬單嗇嗎嘆嘔嘗噁噓噚噴噸噹嚇嚐嚥嚨嚮嚴囂囉囑囪國圍園圓圖團執堅堊報場塊塗塢"
    "塵墜墮墾壇壓壘壞壟壩壯壺壽夠夢夥夾奧奪奮妝姍娛婦媽嬰孫學宮實寧審寫寬寵寶將專尋對"
    "導尷屆屍層屬岡島峽崙嶼巔帥師帳帶幣幫幹幾庫廂廚廟廠廢廣廳張強彈彌彎彙彿後徑從復徵"
    "徹恆恥悅悵悶悽惡惱愛態慘慣慮慶慾憂憐憑憚憤憫憲憶懇應懲懶懷懸懺懼戀戰戲戶拋捨捲掃"
    "掙掛採揚換揮損搖搶摯摻撈撐撓撥撫撲擁擇擊擋擔據擠擬擰擱擲擴擺擾攔攙攜攝攪敗敘敵數"
    "斷於昇時暈暢暫曆曇曉曠曬書會朧東桿條棄棟棧棲楓業極榮構槍槓槳樁樂樑樓標樞樣樸樹橋"
    "機橫檔檢檳櫃櫚權欽歐歡歲歷歸殘殺殼毀毆氣氾汙決沒沖況洩洶涼淨淪淵淺減渦測渾湊湧湯"
    "準溝溫滅滯滲滾滿漁漢漲漸漿潑潔潛潤潰澀澤澱濁濃濕濟濫濱濾瀆瀏瀟瀰灑灘灣災為烏無煉"
    "煙煩熱熾燈燉燒營燦燭燴爍爐爛爭爺爾牆牽犧狀狹猶獄獅獎獨獲獵獸獻現瑣瑩瑪環產畝畢畫"
    "異當疊瘋瘓瘧療癒癟癮癱發皺盜盡監盤盪眥眾瞞矯碩確碼磯礎礙礦礫祕禍禦禪禮禱禿稅稜種"
    "稱穀積穩穫窮窺竅竊競筆節範築篩簡簽簾籃籌籠籬籲糞糧糾紀約紅紋納紐純紗紙級紛紡紮細"
    "紳紹終絃組結絕絡給絨統絲綁經綜綠綢維綱網綽綿緊緒線締緣編緩緬緯練緻縛縣縫縮縱縷總"
    "績繃織繞繩繪繭繳繼續纏纖纜罰罵罷羅羨義羶習聖聞聯聰聲聳職聽聾肅脅脈脫脹腦腫腳腸膚"
    "膠膩膽膾膿臉臘臟臥臨臺與興舉舊艦艱艷芻茲莊莖華菸萊萬葉著蒼蓋蓮蔭蕩蕪蕭薦薩藉藍藝"
    "藥藹蘇蘊蘭處虛虜號虧蛻蝕蝦蝸螢蟲蟻蠔蠟蠱蠻術衛衝補裝裡製複褲褻襪襬襲見規覓視親覺"
    "覽觀觸訂計訊討訓訕託記訝訟訣訪設許訴訶診註詆詐評詛詞詢試詩詭詮話該詳詼誇誌認誕誘"
    "語誠誤誦誨說誰課誹調諂談請諒論諜諧諷諸諺諾謀謂謅謊謎謗謙講謝謠謫謬謹謾證譏識譜譯"
    "議譴護譽讀變讓讚豈豐豬貓貝貞負財貢貧貨販貪貫責貴貶買貸費貼貿賁賂賄資賊賓賜賞賠賢"
    "賣賤賦質賬賭賴賺購賽贅贈贊贏贓贖趕趨跡踰蹟蹣蹤躊躍躪軀車軌軍軛軟軸軼較載輒輓輔輕"
    "輛輝輩輪輯輳輸輻轄轉轟辦辭辯農迴這連週進遊運過達違遙遜遞遠適遲遷選遺邁還邊邏郵鄉"
    "鄰醜醞醫釀釁釋釐釘針釣鈍鈔鈣鈴鉅鉚鉛銀銅銘銜銳銷鋒鋪鋼錄錐錘錢錦錫錯鍊鍋鍚鍛鍵鎖"
    "鎗鎮鏟鏡鐘鐵鑑鑣鑽長門閃閉開閒間閡閣閥閱闆闊關闡陣陰陳陸陽隊階隕際隨險隱隸隻雖雙"
    "雜雞離難雲電霧靂靄靈靜鞏韋韌韓響頁頂項順須頌預頑頒頓頗領頭頸頹頻顆題額顎顏願顛類"
    "顧顫顯風颶飄飆飛飢飪飯飲飼飽飾餅養餌餒餓餘餚館餵餾饋饑饒馬馴駁駐駕駛駭騎騙騰騷驅"
    "驕驗驚驟驢髒體髮鬆鬍鬚鬥鬧鬱魚魯鮑鮮鯊鯡鯨鰓鱈鱒鳥鳩鴉鴿鵜鵝鶘鶴鷹鹹鹼鹽麗麥麩麵"
    "麼黃點黨齊齒齡齣齧龍龐龜"
)
_SIMPLIFIED = (
    "丢并干乱亚布占并来仑侣系具仓个们幸伦伟侧侦伪杰伞备家佣传债伤倾仅仆侥雇价仪亿俭侪"
    "尽偿优储㑩兑儿内两册冻别删则克刚剥创划剧剂劲动务胜劳势励劝汇区协却厌厉参丛员念问"
    "启唤丧乔单啬吗叹呕尝𫫇嘘㖊喷吨当吓尝咽咙向严嚣啰嘱囱国围园圆图团执坚垩报场块涂坞"
    "尘坠堕垦坛压垒坏垄坝壮壶寿够梦伙夹奥夺奋妆姗娱妇妈婴孙学宫实宁审写宽宠宝将专寻对"
    "导尴届尸层属冈岛峡仑屿巅帅师帐带币帮干几库厢厨庙厂废广厅张强弹弥弯汇佛后径从复征"
    "彻恒耻悦怅闷凄恶恼爱态惨惯虑庆欲忧怜凭惮愤悯宪忆恳应惩懒怀悬忏惧恋战戏户抛舍卷扫"
    "挣挂采扬换挥损摇抢挚掺捞撑挠拨抚扑拥择击挡担据挤拟拧搁掷扩摆扰拦搀携摄搅败叙敌数"
    "断于升时晕畅暂历昙晓旷晒书会胧东杆条弃栋栈栖枫业极荣构枪杠桨桩乐梁楼标枢样朴树桥"
    "机横档检槟柜榈权钦欧欢岁历归残杀壳毁殴气泛污决没冲况泄汹凉净沦渊浅减涡测浑凑涌汤"
    "准沟温灭滞渗滚满渔汉涨渐浆泼洁潜润溃涩泽淀浊浓湿济滥滨滤渎浏潇弥洒滩湾灾为乌无炼"
    "烟烦热炽灯炖烧营灿烛烩烁炉烂争爷尔墙牵牺状狭犹狱狮奖独获猎兽献现琐莹玛环产亩毕画"
    "异当叠疯痪疟疗愈瘪瘾瘫发皱盗尽监盘荡眦众瞒矫硕确码矶础碍矿砾秘祸御禅礼祷秃税棱种"
    "称谷积稳获穷窥窍窃竞笔节范筑筛简签帘篮筹笼篱吁粪粮纠纪约红纹纳纽纯纱纸级纷纺扎细"
    "绅绍终弦组结绝络给绒统丝绑经综绿绸维纲网绰绵紧绪线缔缘编缓缅纬练致缚县缝缩纵缕总"
    "绩绷织绕绳绘茧缴继续缠纤缆罚骂罢罗羡义膻习圣闻联聪声耸职听聋肃胁脉脱胀脑肿脚肠肤"
    "胶腻胆脍脓脸腊脏卧临台与兴举旧舰艰艳刍兹庄茎华烟莱万叶着苍盖莲荫荡芜萧荐萨借蓝艺"
    "药蔼苏蕴兰处虚虏号亏蜕蚀虾蜗萤虫蚁蚝蜡蛊蛮术卫冲补装里制复裤亵袜䙓袭见规觅视亲觉"
    "览观触订计讯讨训讪托记讶讼诀访设许诉诃诊注诋诈评诅词询试诗诡诠话该详诙夸志认诞诱"
    "语诚误诵诲说谁课诽调谄谈请谅论谍谐讽诸谚诺谋谓诌谎谜谤谦讲谢谣谪谬谨谩证讥识谱译"
    "议谴护誉读变让赞岂丰猪猫贝贞负财贡贫货贩贪贯责贵贬买贷费贴贸贲赂贿资贼宾赐赏赔贤"
    "卖贱赋质账赌赖赚购赛赘赠赞赢赃赎赶趋迹逾迹蹒踪踌跃躏躯车轨军轭软轴轶较载辄挽辅轻"
    "辆辉辈轮辑辏输辐辖转轰办辞辩农回这连周进游运过达违遥逊递远适迟迁选遗迈还边逻邮乡"
    "邻丑酝医酿衅释厘钉针钓钝钞钙铃钜铆铅银铜铭衔锐销锋铺钢录锥锤钱锦锡错炼锅钖锻键锁"
    "枪镇铲镜钟铁鉴镳钻长门闪闭开闲间阂阁阀阅板阔关阐阵阴陈陆阳队阶陨际随险隐隶只虽双"
    "杂鸡离难云电雾雳霭灵静巩韦韧韩响页顶项顺须颂预顽颁顿颇领头颈颓频颗题额颚颜愿颠类"
    "顾颤显风飓飘飙飞饥饪饭饮饲饱饰饼养饵馁饿余肴馆喂馏馈饥饶马驯驳驻驾驶骇骑骗腾骚驱"
    "骄验惊骤驴脏体发松胡须斗闹郁鱼鲁鲍鲜鲨鲱鲸鳃鳕鳟鸟鸠鸦鸽鹈鹅鹕鹤鹰咸碱盐丽麦麸面"
    "么黄点党齐齿龄出啮龙庞龟"
)
assert len(_TRADITIONAL) == len(_SIMPLIFIED)
CHARACTERS = str.maketrans(_TRADITIONAL, _SIMPLIFIED)
# ICU gives these a rare form outside the basic CJK block (儸 → 㑩); the common
# simplification is used instead (侏罗纪, 恶心, 英寻, 下摆).  ICU keeps the animal
# pronoun 牠, which Simplified writes 它.
CHARACTERS.update(str.maketrans("儸噁噚襬牠", "罗恶寻摆它"))

# Phrases in which a character keeps its form or takes another simplification
# than its default (著 → 着, 畫 → 画 by default); their text is final.
PHRASES = {
    "著名": "著名", "著稱": "著称", "著作": "著作", "原著": "原著", "名著": "名著", "顯著": "显著",
    "土著": "土著", "昭著": "昭著", "卓著": "卓著", "著者": "著者", "編著": "编著",
    "計畫": "计划", "規畫": "规划", "策畫": "策划", "企畫": "企划", "畫分": "划分", "畫一的": "划一的",
    "份子": "分子", "螢幕": "荧幕", "彷彿": "仿佛", "英吋": "英寸",
}
_PHRASE = re.compile("|".join(sorted(PHRASES, key=len, reverse=True)))


def to_simplified(text: str) -> str:
    """The text with Traditional characters replaced by Simplified ones."""

    if not text:
        return text
    parts: list[str] = []
    start = 0
    for match in _PHRASE.finditer(text):
        parts.append(text[start:match.start()].translate(CHARACTERS))
        parts.append(PHRASES[match.group(0)])
        start = match.end()
    parts.append(text[start:].translate(CHARACTERS))
    return "".join(parts)


__all__ = ["CHARACTERS", "PHRASES", "to_simplified"]
