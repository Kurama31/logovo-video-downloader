import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chooseLanguage} from '../app/web/language-policy.js';
import {translateMessage} from '../app/web/translation-core.js';
import {english} from '../app/web/translations-en.js';
const t=value=>translateMessage(value,'en');

test('manual choice precedes browser languages; unsupported choices are ignored',()=>{
  assert.equal(chooseLanguage('ru',['en-US']),'ru');
  assert.equal(chooseLanguage('en',['ru-RU']),'en');
  assert.equal(chooseLanguage('invalid',['ru-RU']),'ru');
});
test('automatic choice follows browser priorities and landing aliases',()=>{
  for(const tag of ['be-BY','uk-UA','kk-KZ','hy-AM','ka-GE','tg-TJ'])assert.equal(chooseLanguage(null,[tag,'en']),'ru');
  assert.equal(chooseLanguage(null,['en-US','ru']),'en');
  assert.equal(chooseLanguage(null,['de','ru-RU','en']),'ru');
  assert.equal(chooseLanguage(null,['ja-JP']),'en');
  assert.equal(chooseLanguage(null,[]),'en');
});
test('all static UI texts and accessible labels have an English translation',()=>{
  const html=readFileSync(new URL('../app/web/index.html',import.meta.url),'utf8');
  for(const match of html.matchAll(/data-i18n(?:-aria-label|-title|-placeholder)?="([^"]+)"/g)){
    const text=match[1].replaceAll('&amp;','&').replaceAll('&quot;','"').trim();
    assert.ok(Object.hasOwn(english,text),'Missing translation: '+text);
  }
});
test('dynamic progress, errors, ranges and stored history render in English',()=>{
  assert.equal(t('Проверяем: завершено 50% этапов проверки'),'Checking: 50% of check stages complete');
  assert.equal(t('Скачать нельзя: не удалось получить видео по вашей ссылке'),'Cannot download: could not retrieve the video from your link');
  assert.equal(t('Не скачалось из-за ошибки (Источник требует авторизацию (HTTP 401).)'),'Download failed due to an error (The source requires authentication (HTTP 401).)');
  assert.equal(t('Доступные серии: с 1 по 12, 15. Всего в озвучке: 13.'),'Available episodes: 1–12, 15. Total for this audio version: 13.');
  assert.equal(t('1.0 МБ / 2.0 МБ · 10.0 КБ/с'),'1.0 MB / 2.0 MB · 10.0 KB/s');
  assert.equal(t('Озвучка AniLibria · 12 серий'),'Audio: AniLibria · 12 episodes');
});
test('source filenames and user content are not translated or interpreted as markup',()=>{
  assert.equal(t('Передан в загрузки браузера: «Русское видео <test>.mp4». Фактическую папку смотрите в загрузках браузера.'),'Sent to browser downloads: “Русское видео <test>.mp4”. Check browser downloads for the actual folder.');
  assert.equal(t('Это название моего видео'),'Это название моего видео');
  assert.equal(t('Передан в загрузки браузера: «Моё · видео.mp4». Фактическую папку смотрите в загрузках браузера.'),'Sent to browser downloads: “Моё · видео.mp4”. Check browser downloads for the actual folder.');
  assert.equal(translateMessage('Скачать','ru'),'Скачать');
});
