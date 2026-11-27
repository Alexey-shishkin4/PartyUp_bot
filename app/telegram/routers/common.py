from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message

from app.config import RESOURCES
from app.telegram.ui import buttons, menu


def build_common_router(profiles, settings):
    router = Router(name="common")

    @router.message(Command("privacy"))
    async def privacy(message: Message):
        await message.answer(
            "<b>Данные и видимость</b>\n"
            "Храним анкету, Telegram ID, username, ссылку Telegram на фото анкеты, предпочтения, выбор игр, реакции, жалобы и события показов. "
            "После подключения Steam храним SteamID, доступную библиотеку и игровое время для подбора. "
            "При импорте Steam передаём SteamID в OpenDota для проверки Dota 2. "
            "При наличии наигрыша в CS2 или Deadlock также запрашиваем их статистику через FACEIT (или WOK) и Deadlock API. "
            "В PostgreSQL сохраняем ранги, доступные часы, счётчики матчей и другие данные источников.\n\n"
            "При добавлении PUBG, Fortnite или Riot передаём указанный ник или ID поставщикам статистики. "
            "Храним игровые идентификаторы, серверы, ранги, время по режимам, матчи и источники. "
            "Привязки можно удалить через «Добавить игры» → «Управлять привязками».\n\n"
            "В карточке показываем загруженное фото, имя, часовой пояс, описание и список игр с доступными рангами. "
            "Наигрыш, счётчики матчей и игровые идентификаторы остаются в базе. "
            "Людям в подборке также показываем до пяти общих с ними игр, повлиявших на подбор. "
            "Telegram ID и SteamID публично не показываем. С твоего согласия при взаимном лайке бот покажет собеседнику твой @username. "
            "Назначенные операторы и разработчики могут просматривать и исправлять анкеты через закрытую панель; изменения журналируются. "
            "После совпадения общение идёт напрямую в личных сообщениях Telegram.\n\n"
            f"Хранение: {escape(settings.data_location)}. Контакт: {escape(settings.operator_contact)}.\n"
            "Анкеты без активности 30 дней скрываются. /pause — пауза, /steam — отключение Steam, "
            "/photo — заменить или убрать фото, /delete — удаление анкеты и связанных записей из рабочей БД. "
            "Сами фото хранятся в Telegram; при удалении из анкеты убирается наша ссылка на фото. "
            "Доставленные сообщения и фото у адресатов остаются. "
            "Если оператор делает резервные копии, срок их удаления должен быть указан им отдельно."
        )

    @router.message(Command("about"))
    async def about(message):
        await message.answer(
            "PartyUp · одна анкета, единые настройки, общие игры. 18+.\n"
            "Подбор по игровой истории: главная общая игра 60%, другие общие игры 30%, жанры 10%. "
            "При равном балле выше анкета с большим пересечением расписания.\n"
            "Отдельно доступна исследовательская модель на Steam Video Games (Tamber, Kaggle, 2017), "
            "ODbL 1.0 / DbCL 1.0.\n"
            '<a href="https://www.kaggle.com/datasets/tamber/steam-video-games">Источник данных</a> · '
            "/model — скачать модель и отчёт"
        )

    @router.message(Command("model"))
    async def download_model(message):
        await message.answer_document(
            FSInputFile(RESOURCES / "affinity.json"),
            caption=(
                "Производная база связей игр · Steam Video Games / Tamber, Kaggle. "
                "ODbL 1.0 / DbCL 1.0. Источник: https://www.kaggle.com/datasets/tamber/steam-video-games\n"
                "Лицензии: https://opendatacommons.org/licenses/odbl/1-0/ и "
                "https://opendatacommons.org/licenses/dbcl/1-0/"
            ),
        )
        await message.answer_document(
            FSInputFile(RESOURCES / "model_report.json"),
            caption="Метрики и ограничения модели игровых интересов",
        )

    @router.message(Command("cancel"))
    async def cancel(message, state: FSMContext):
        await state.clear()
        await message.answer("Вернулись в меню.", reply_markup=menu())

    @router.message(Command("delete"))
    async def delete_prompt(message, state: FSMContext):
        await state.clear()
        await message.answer(
            "Удалить анкету, подключённую игровую статистику, реакции, совпадения и черновик?",
            reply_markup=buttons([[("Удалить мои данные", "delete:yes"), ("Отмена", "delete:no")]]),
        )

    @router.callback_query(F.data.startswith("delete:"))
    async def delete_confirm(callback, state: FSMContext):
        await callback.answer()
        if callback.data == "delete:yes":
            await state.clear()
            profiles.delete_user(callback.from_user.id)
            await callback.message.answer("Данные удалены. /start — создать новую анкету.")
        else:
            await callback.message.answer("Удаление отменено.", reply_markup=menu())
        await callback.message.edit_reply_markup(reply_markup=None)

    return router
