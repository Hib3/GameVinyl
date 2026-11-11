// Uses the same local read-only API as the catalogue. No account/session data is collected.
labels.AUCTION_OPEN = '入札候補（観測時点）';
labels.AUCTION_ENDED = '終了';
labels.watch_new = '重要盤の新着候補';

const searchRoutes = [
  ['国内の中古・オークション', [
    ['Yahoo!オークション（公式通知あり）','https://auctions.yahoo.co.jp/search/search?p=', true],
    ['Yahoo!フリマ','https://paypayfleamarket.yahoo.co.jp/search/'],
    ['メルカリ','https://jp.mercari.com/search?keyword='],
    ['楽天ラクマ','https://fril.jp/s?query='],
    ['駿河屋','https://www.suruga-ya.jp/search?search_word='],
    ['ディスクユニオン','https://diskunion.net/portal/ct/list/0/0/0/0/0/0/0/0/1?kw='],
  ]],
  ['海外の市場・専門店', [
    ['Discogs','https://www.discogs.com/search/?type=all&q='],
    ['eBay','https://www.ebay.com/sch/i.html?_nkw='],
    ['Juno Records','https://www.juno.co.uk/search/?q%5Ball%5D%5B%5D='],
    ['Bandcamp','https://bandcamp.com/search?q='],
  ]],
  ['告知・SNS（出品の根拠にはしない）', [
    ['X','https://x.com/search?f=live&q='],
    ['Bluesky','https://bsky.app/search?q='],
    ['Reddit','https://www.reddit.com/search/?sort=new&q='],
    ['YouTube','https://www.youtube.com/results?search_query='],
    ['Instagramの公開ページ','https://www.google.com/search?q=site%3Ainstagram.com+'],
    ['Facebookの公開ページ','https://www.google.com/search?q=site%3Afacebook.com+'],
    ['未登録店舗も検索','https://www.google.com/search?q='],
  ]],
];
function showRoutes(watches) {
  const box = $('watchRoutes');
  if (box.childElementCount) return;
  for (const watch of watches) {
    box.append(el('h3',watch.title));
    for (const [group, sites] of searchRoutes) {
      const items = el('ul');
      for (const [name,base,local] of sites) {
        const node = el('li');
        const term = local ? watch.search : watch.terms[0].join(' ');
        node.append(link(base+encodeURIComponent(term),name));
        items.append(node);
      }
      box.append(el('p',group),items);
    }
    box.append(el('p','上のリンクは確認先です。Yahoo!オークションの設定済み検索と登録店舗以外は、巡回・取得済みとして数えません。','muted'));
  }
}
function notifyWatch(alerts) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const seen = Number(localStorage.getItem('vgm-watch-last-alert') || '0');
  const fresh = alerts.filter(a=>a.id>seen);
  if (!fresh.length) return;
  try {
    const n = new Notification('ゲーム音楽レコード：新着候補 '+fresh.length+'件', {body:fresh[0].title});
    n.onclick = () => { window.focus(); $('watch').scrollIntoView(); };
    localStorage.setItem('vgm-watch-last-alert',String(Math.max(...fresh.map(a=>a.id))));
  } catch {
    $('watchPermission').textContent = 'このブラウザでは通知を表示できません。下の公式通知を利用してください。';
  }
}
async function loadWatch() {
  try {
    const d = await get('/api/watch');
    showRoutes(d.watches);
    const current = d.offers.filter(o=>o.current);
    $('watchStatus').textContent = d.watches.map(w=>w.title).join('、')+'：現在の候補 '+current.length+'件';
    const box = $('watchOffers');
    box.replaceChildren();
    for (const c of d.checks) {
      const status = c.last_error ? '取得失敗：'+c.last_error : c.last_success ? '最終成功 '+date(c.last_success)+' · 一致 '+c.matches+'件' : '未確認';
      const line = el('p',undefined,'muted');
      line.append(link(c.url,new URL(c.url).searchParams.get('p')||'Yahoo!オークション'),document.createTextNode('：'+status));
      box.append(line);
    }
    if (!d.offers.length) box.append(el('p','出品の観測記録はまだありません。'));
    for (const o of [...current,...d.offers.filter(o=>!o.current)].slice(0,20)) {
      const f = o.facts, row = el('div',undefined,'offer');
      const value = f.auction_price ? f.auction_price+' JPY（'+(f.availability==='AUCTION_ENDED'?'終了時表示':'観測した入札額')+'）' : price(f);
      row.append(link(o.url,f.title),el('p',(labels[f.availability]||f.availability)+' · '+value+(f.review_flags.includes('incomplete_set')?' · 欠品あり':'')),
        el('p','観測 '+date(o.observed)+(f.auction_end?' · 終了日時 '+date(f.auction_end):'')+(o.current?'':' · 現在購入可能な候補には含めません。'),'muted'));
      const detail = el('button','根拠を見る');
      detail.onclick = ()=>showEvidence(o);
      row.append(detail);
      box.append(row);
    }
    notifyWatch(d.alerts);
  } catch {
    $('watchStatus').textContent = 'ウォッチの読み取りに失敗しました。候補ゼロとは判定しません。';
  }
}
$('watchNotify').onclick = async () => {
  if (!('Notification' in window)) {
    $('watchPermission').textContent = 'このブラウザはデスクトップ通知に対応していません。';
    return;
  }
  const result = await Notification.requestPermission();
  $('watchPermission').textContent = result==='granted' ? '通知を許可しました。このページを開いている間に新着候補を通知します。' : '通知は許可されていません。ブラウザ設定または公式通知を利用してください。';
  if (result==='granted') loadWatch();
};
loadWatch();
setInterval(loadWatch,30000);
