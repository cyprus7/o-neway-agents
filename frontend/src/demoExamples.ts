import templates from './demoExamples.json';

export function demoExamples(today = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Nicosia', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(today);
  const part = (type: string) => Number(parts.find(value => value.type === type)?.value);
  const year = part('year') + (part('month') > 11 || (part('month') === 11 && part('day') > 20) ? 1 : 0);
  return templates.map(example => ({ ...example, text: example.text.replace('{year}', String(year)) }));
}
