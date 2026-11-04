import { Check, LoaderCircle, RotateCcw } from 'lucide-react';
import type { ConnectionStatus as Status } from './state';
import './connection.css';

export function ConnectionStatus({ status, retry }: { status: Status; retry: () => void }) {
  const live = status === 'live';
  return <div className={`live-connection live-connection-${status}`}>
    <span className="live-connection-message" role="status" aria-live="polite" aria-atomic="true">
      {live ? <Check size={13} aria-hidden="true"/> : <LoaderCircle size={13} className="spin" aria-hidden="true"/>}
      <span>{live ? 'Обновления подключены' : status === 'connecting' ? 'Подключаем обновления…' : 'Восстанавливаем обновления…'}</span>
    </span>
    {!live && <button onClick={retry} className="live-retry"><RotateCcw size={13} aria-hidden="true"/> Подключить снова</button>}
  </div>;
}
