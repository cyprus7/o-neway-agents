import { BookOpen, ExternalLink } from 'lucide-react';
import type { Component } from '../types';
import './catalog-facts.css';

function osmRecord(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' && url.hostname === 'www.openstreetmap.org' && !url.username && !url.password
      && /^\/(node|way|relation)\/[0-9]+$/.test(url.pathname)) return url.href;
  } catch { /* An invalid source is not a navigable link. */ }
  return undefined;
}

function snapshotTime(value: unknown): string {
  if (typeof value !== 'string' || Number.isNaN(Date.parse(value))) return 'время не указано';
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Nicosia' }).format(new Date(value));
}

const attributes: [string, string][] = [
  ['stars', 'Звёзды по OSM'], ['swimming_pool', 'Бассейн'], ['wheelchair', 'Доступность для коляски'],
  ['pets', 'Питомцы'], ['dog', 'Собаки'], ['internet_access', 'Интернет'], ['air_conditioning', 'Кондиционер'],
];

const attributeValue = (value: string) => ({ yes: 'Да', no: 'Нет', limited: 'Ограниченно', wlan: 'Wi-Fi',
  free: 'Бесплатно', customers: 'Для гостей', terminal: 'Терминал' }[value] || value);

export function CatalogFacts({ part }: { part: Component }) {
  const details = part.details;
  if (details?.discovery_mode !== 'local_catalog') return null;
  const tags = details.osm_tags && typeof details.osm_tags === 'object' && !Array.isArray(details.osm_tags)
    ? details.osm_tags as Record<string, unknown> : {};
  const known = attributes.flatMap(([key, label]) => typeof tags[key] === 'string' && tags[key] !== ''
    ? [{ key, label, value: tags[key] as string }] : []).slice(0, 4);
  const source = osmRecord(part.source_url);
  const alternatives = (Array.isArray(details.alternative_properties) ? details.alternative_properties : [])
    .flatMap(value => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
      const row = value as Record<string, unknown>;
      const url = osmRecord(row.source_url);
      return typeof row.name === 'string' && row.name.trim() && url ? [{ name: row.name, url }] : [];
    }).slice(0, 2);
  return <section className="catalog-facts" aria-label="Сведения из локального каталога">
    <span className="catalog-snapshot"><BookOpen size={14} aria-hidden="true"/> Локальный каталог · снимок {snapshotTime(details.catalog_retrieved_at)} (Кипр)</span>
    {known.length > 0 && <dl className="catalog-attributes">{known.map(attribute => <div key={attribute.key} title={`OpenStreetMap: ${attribute.key}=${attribute.value}`}><dt>{attribute.label}</dt><dd>{attributeValue(attribute.value)}</dd></div>)}</dl>}
    <p className="catalog-caution">Отметки сообщества, условия не проверены. {!known.length && 'Сведения об удобствах в этой записи не указаны.'}</p>
    <div className="catalog-source-links">{source && <a href={source} target="_blank" rel="noreferrer">Запись OpenStreetMap <ExternalLink size={12} aria-hidden="true"/></a>}<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© участники OSM · ODbL</a></div>
    {alternatives.length > 0 && <details className="catalog-alternatives"><summary>Ещё {alternatives.length === 1 ? 'один объект' : '2 объекта'} в локальном каталоге</summary><p>Справочные записи поблизости. Цены и наличие не проверены.</p><ul>{alternatives.map(alternative => <li key={alternative.url}><a href={alternative.url} target="_blank" rel="noreferrer">{alternative.name}<ExternalLink size={12} aria-hidden="true"/></a></li>)}</ul></details>}
  </section>;
}
