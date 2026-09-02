// Inline SVG icons in the Open-WebUI / Lucide visual style.
// All icons are 1em-sized and inherit currentColor — drive them via
// `text-zinc-300` / `text-zinc-500` etc. on the parent.

import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number | string };

function svg(d: string) {
  return function Icon({ size = 22, ...rest }: IconProps) {
    return (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.85"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        {...rest}
      >
        <path d={d} />
      </svg>
    );
  };
}

function svgMulti(paths: string[]) {
  return function Icon({ size = 22, ...rest }: IconProps) {
    return (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.85"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        {...rest}
      >
        {paths.map((p, i) => (
          <path key={i} d={p} />
        ))}
      </svg>
    );
  };
}

export const NewChatIcon = svgMulti([
  "M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7",
  "M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4 9.5-9.5z",
]);

export const SearchIcon = svgMulti([
  "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z",
  "m21 21-4.3-4.3",
]);

export const FolderIcon = svg(
  "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"
);

export const ChatBubbleIcon = svg(
  "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"
);

export const SettingsIcon = svgMulti([
  "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z",
  "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
]);

export const LogOutIcon = svgMulti([
  "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4",
  "m16 17 5-5-5-5",
  "M21 12H9",
]);

export const SendIcon = svgMulti(["M5 12h14", "m12 5 7 7-7 7"]);

export const StopIcon = ({ size = 20 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <rect x="6" y="6" width="12" height="12" rx="2" />
  </svg>
);

export const SlashIcon = ({ size = 20 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
    <path d="M9 20l6-16" />
  </svg>
);

export const SkillsIcon = ({ size = 20 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <path d="M12 2l2.4 7.2H22l-6 4.4 2.3 7.2-6.3-4.6L5.7 20.8 8 13.6 2 9.2h7.6z" />
  </svg>
);

// Provider logos. Kept as plain functional components (not the `svg()`
// factory) because they carry brand fills, not currentColor strokes.
//
// Настоящий логотип Claude — асимметричный терракотовый «солнечный» бёрст
// Anthropic. Единый <path> из официального ассета (Simple Icons, CC0),
// viewBox 0 0 24 24, бренд-цвет #D97757. НЕ рисовать вручную из палок/звёзд:
// раньше тут был самодельный симметричный бёрст из 12 одинаковых прямоугольников
// («сгенерированная фигня») — его и просили заменить настоящим знаком.
const CLAUDE_MARK_PATH =
  "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z";
export const ClaudeLogo = ({ size = 16 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="#D97757" aria-hidden role="img">
    <path d={CLAUDE_MARK_PATH} />
  </svg>
);

export const OpenAILogo = ({ size = 16 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <circle cx="12" cy="12" r="9" />
  </svg>
);

/** Picks the provider logo from a model id. Defaults to Claude. */
export function providerLogo(modelId: string | undefined, size = 16) {
  if (modelId && /gpt|openai|o1|o3/i.test(modelId)) return <OpenAILogo size={size} />;
  return <ClaudeLogo size={size} />;
}

export const PlusIcon = svgMulti(["M12 5v14", "M5 12h14"]);

export const SparklesIcon = svgMulti([
  "m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3L12 3z",
]);

export const MicIcon = svgMulti([
  "M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z",
  "M19 10v2a7 7 0 0 1-14 0v-2",
  "M12 19v3",
]);

export const PanelLeftIcon = svgMulti([
  "M3 4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  "M9 2v20",
]);

export const ChevronRightIcon = svg("m9 6 6 6-6 6");
export const ChevronDownIcon = svg("m6 9 6 6 6-6");

export const TrashIcon = svgMulti([
  "M3 6h18",
  "M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6",
  "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
]);

export const RefreshIcon = svgMulti([
  "M3 12a9 9 0 0 1 15-6.7L21 8",
  "M21 3v5h-5",
  "M21 12a9 9 0 0 1-15 6.7L3 16",
  "M3 21v-5h5",
]);

export const CloseIcon = svgMulti(["M18 6 6 18", "M6 6l12 12"]);

export const NotesIcon = svgMulti([
  "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z",
  "M14 2v6h6",
  "M16 13H8",
  "M16 17H8",
  "M10 9H8",
]);

// Артефакты — коробка-«пакет» (стопка сохранённых файлов Claude).
export const PackageIcon = svgMulti([
  "m7.5 4.27 9 5.15",
  "M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z",
  "M3.3 7 12 12l8.7-5",
  "M12 22V12",
]);

// Размышления — лампочка (идея/рассуждение).
export const LightbulbIcon = svgMulti([
  "M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.8 1.3 1.5 1.5 2.5",
  "M9 18h6",
  "M10 22h4",
]);

// Документация — раскрытая книга.
export const BookIcon = svg(
  "M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20"
);

// Файл (документ с загнутым углом).
export const FileIcon = svgMulti([
  "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z",
  "M14 2v6h6",
]);

// Файлы проекта — две «страницы» (стопка документов). Намеренно отличается от
// FolderIcon: в шапке кнопка «Файлы» не должна повторять глиф папки проекта
// из бредкрамба (иначе два одинаковых значка с разным смыслом в одном ряду).
export const UsersIcon = svgMulti([
  "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2",
  "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  "M22 21v-2a4 4 0 0 0-3-3.87",
  "M16 3.13a4 4 0 0 1 0 7.75",
]);

export const FilesIcon = svgMulti([
  "M15.5 2H8.6c-.4 0-.8.2-1.1.5-.3.3-.5.7-.5 1.1v12.8c0 .4.2.8.5 1.1.3.3.7.5 1.1.5h9.8c.4 0 .8-.2 1.1-.5.3-.3.5-.7.5-1.1V6.5L15.5 2z",
  "M3 7.6v12.8c0 .4.2.8.5 1.1.3.3.7.5 1.1.5h9.8",
  "M15 2v5h5",
]);

// Картинка.
export const ImageIcon = svgMulti([
  "M19 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z",
  "M8.5 11a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z",
  "m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21",
]);

// Карандаш — переименовать/редактировать.
export const EditIcon = svgMulti([
  "M12 20h9",
  "M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z",
]);

// Папка с «плюсом» — создать папку.
export const FolderPlusIcon = svgMulti([
  "M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z",
  "M12 10v6",
  "M9 13h6",
]);

// Галочка — подтвердить (сохранить).
export const CheckIcon = svg("M20 6 9 17l-5-5");

// Скачать — стрелка в лоток.
export const DownloadIcon = svgMulti([
  "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4",
  "M7 10l5 5 5-5",
  "M12 15V3",
]);

// Развернуть/свернуть панель (две стрелки в стороны / внутрь).
export const MaximizeIcon = svgMulti([
  "M8 3H5a2 2 0 0 0-2 2v3",
  "M21 8V5a2 2 0 0 0-2-2h-3",
  "M3 16v3a2 2 0 0 0 2 2h3",
  "M16 21h3a2 2 0 0 0 2-2v-3",
]);

// Стрелка-ответ — «В Telegram» (deeplink назад в бота).
export const ReplyIcon = svgMulti([
  "M20 18v-2a4 4 0 0 0-4-4H4",
  "m9 17-5-5 5-5",
]);

// Вопрос в круге — «Claude уточняет».
export const HelpCircleIcon = svgMulti([
  "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
  "M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3",
  "M12 17h.01",
]);
