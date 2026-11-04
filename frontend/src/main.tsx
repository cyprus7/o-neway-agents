import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { bootstrapTelegram } from './telegram/bootstrap';
import './styles.css';

const container = document.getElementById('root')!;
const loading = document.createElement('p');
loading.setAttribute('role', 'status');
loading.textContent = 'Открываем приложение…';
container.replaceChildren(loading);
void bootstrapTelegram().then(() => {
  ReactDOM.createRoot(container).render(<React.StrictMode><App /></React.StrictMode>);
}).catch(() => {
  // Do not render App on a failed Telegram launch: its guest bootstrap must not run.
  const screen = document.createElement('main');
  screen.className = 'loading-state';
  const title = document.createElement('h1');
  title.textContent = 'Не удалось открыть Telegram';
  const message = document.createElement('p');
  message.setAttribute('role', 'alert');
  message.textContent = 'Откройте Mini App заново или повторите загрузку.';
  const retry = document.createElement('button');
  retry.className = 'primary-button';
  retry.textContent = 'Повторить загрузку';
  retry.addEventListener('click', () => window.location.reload());
  screen.append(title, message, retry);
  container.replaceChildren(screen);
});
